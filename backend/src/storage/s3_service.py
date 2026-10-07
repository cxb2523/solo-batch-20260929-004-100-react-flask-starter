"""Injectable S3 storage service.

This module collapses the boto3 client construction, bucket probing and
object upload/download that were previously repeated across the
``/localstack`` routes (and needed by Celery tasks) into one abstraction.

Design decisions / explicit tradeoffs
-------------------------------------
1. Client lifecycle: PROCESS-LEVEL SINGLETON (lazy + thread-safe), not
   per-request/per-task construction.
     * A boto3/client wraps a urllib3 ``ConnectionPool``; rebuilding it for
       every request is wasteful, and callers that build a fresh client
       themselves tend to leak pooled connections (there is no request hook
       that reliably closes it). We therefore build the client once and
       expose :meth:`close` for tests / process teardown.
     * Flask sync requests run concurrently on threads and Celery workers
       may run threaded pools as well, so the lazy construction is guarded
       by an ``RLock`` and the cached reference is published only after the
       factory returns successfully. A factory failure is never cached, so
       the next call retries (probe/recovery after LocalStack was down).
     * Celery's default prefork model forks AFTER the worker module is
       imported. The service is therefore never constructed at import time;
       it is built lazily on first use *inside each worker process*. Each
       forked process gets its own singleton - nothing is shared across
       processes (a shared socket inherited across fork would be broken).

2. Wiring: DEPENDENCY INJECTION, not a hidden global factory.
     * The sync routes receive the service through ``app.extensions`` (see
       :func:`src.routes.localstack.init_storage`), which makes per-test
       injection of a fake S3 client trivial.
     * Async tasks get it through a per-process provider
       (:meth:`src.tasks.Tasks.get_storage`), also overridable in tests.
     * A plain ``get_storage()`` global would have been less code, but it
       hides the dependency, is painful to override per test, and - if
       evaluated at import - risks constructing the boto client in the
       Celery master process before fork.

3. Bucket probing: the ``bucket_exists`` result is not negatively cached.
   Only an affirmative "bucket is ready" state is memoized; any probe error
   (LocalStack momentarily unreachable, 404 on a not-yet-created bucket) is
   retried on the next call, so a failed probe self-heals instead of
   pinning the service to an unavailable state for the process lifetime.
"""
import datetime
import os
import threading

from botocore.exceptions import ClientError
from localstack_client.session import Session as LocalStackSession


class S3StorageError(Exception):
    """Raised when storage is unreachable or the object is missing."""


class S3StorageService:
    """Thin, injectable facade over the S3 operations used by the app."""

    DEFAULT_BUCKET = 'test-bucket'

    def __init__(self,
                 bucket=None,
                 localstack_host=None,
                 client_factory=None,
                 client_source=None):
        """
        :param bucket: target bucket name.
        :param localstack_host: LocalStack host for the default factory;
            ``None`` lets ``localstack_client`` resolve ``LOCALSTACK_HOST``.
        :param client_factory: zero-arg callable returning an S3 client.
            Inject it in tests to avoid any network access. When omitted a
            factory building a LocalStack-backed boto3 client is used.
        :param client_source: human-readable label reported by the status
            panel, identifying where the client came from.
        """
        self.bucket = bucket or self.DEFAULT_BUCKET
        self.localstack_host = localstack_host or os.getenv('LOCALSTACK_HOST')

        if client_factory is not None:
            self._client_factory = client_factory
            self.client_source = client_source or 'injected-client-factory'
        else:
            self._client_factory = self._default_client_factory
            self.client_source = (
                client_source
                or 'localstack-session (process-level singleton cache)'
            )

        # See module docstring: single client per process, created lazily.
        self._client = None
        self._client_lock = threading.RLock()

        # Bucket readiness is the only positively cached probe result.
        self._bucket_ready = False
        self._bucket_lock = threading.RLock()

        # Last-upload panel state; in-memory by design (per process).
        self._last_upload = None
        self._state_lock = threading.Lock()

    def _default_client_factory(self):
        # A fresh Session per service instance; the boto client itself is
        # what gets cached and reused. verify=False mirrors LocalStack's
        # self-signed/HTTP endpoint setup.
        session = LocalStackSession(localstack_host=self.localstack_host)
        return session.client('s3')

    # -- client -----------------------------------------------------------

    def get_client(self):
        """Return the process-level cached S3 client, building it on first use.

        Thread-safe: concurrent first callers block on one construction.
        If the factory raises, the failure is deliberately NOT cached so a
        later call after LocalStack recovers gets a fresh, working client.
        """
        client = self._client
        if client is not None:
            return client
        with self._client_lock:
            if self._client is None:
                # Publish only after success; an exception propagates and
                # leaves self._client as None for retry.
                self._client = self._client_factory()
            return self._client

    def close(self):
        """Release the cached client's connection pool.

        Because clients are cached and reused, callers normally never need
        this; it exists for tests and for worker shutdown hooks so pooled
        sockets cannot leak.
        """
        with self._client_lock:
            client = self._client
            self._client = None
            self._bucket_ready = False
        if client is not None and hasattr(client, 'close'):
            client.close()

    # -- bucket probing ---------------------------------------------------

    def bucket_exists(self, bucket=None):
        """Live-probe the bucket. Never caches a negative/error result.

        :returns: ``True``/``False`` for a definitive answer.
        :raises S3StorageError: if LocalStack itself cannot be reached, so
            callers can distinguish "missing" from "don't know".
        """
        name = bucket or self.bucket
        client = self.get_client()
        try:
            client.head_bucket(Bucket=name)
            return True
        except ClientError as exc:
            code = exc.response.get('Error', {}).get('Code')
            status = exc.response.get('ResponseMetadata', {}).get(
                'HTTPStatusCode')
            if code in ('404', 'NoSuchBucket', 'Not Found') or status == 404:
                return False
            # AccessDenied (403) technically implies the bucket exists, but
            # anything else (connection-level errors surface as subclasses
            # of BotoCoreError, not ClientError) is a probe failure.
            if code in ('403', 'AccessDenied'):
                return True
            raise S3StorageError('bucket probe failed: {}'.format(exc)) from exc
        except Exception as exc:
            # EndpointConnectionError / ConnectTimeoutError etc.
            raise S3StorageError(
                'storage endpoint unreachable: {}'.format(exc)) from exc

    def ensure_bucket(self, bucket=None):
        """Make sure the bucket exists, creating it if necessary.

        Retry-friendly: connection errors propagate but no negative state is
        cached, so simply calling again after LocalStack is back recovers.
        """
        name = bucket or self.bucket
        if self._bucket_ready and name == self.bucket:
            return
        with self._bucket_lock:
            if self._bucket_ready and name == self.bucket:
                return
            try:
                exists = self.bucket_exists(name)
            except S3StorageError:
                # Probe failed (endpoint down). Do NOT flip _bucket_ready;
                # the next call retries the probe transparently.
                raise
            if not exists:
                self.get_client().create_bucket(Bucket=name)
            # Only a confirmed create/head marks readiness.
            self._bucket_ready = True

    # -- object upload / download -----------------------------------------

    def upload_file(self, key, body, bucket=None, content_type=None):
        """Upload one object and remember the outcome for the status panel."""
        name = bucket or self.bucket
        if isinstance(body, str):
            body = body.encode('utf-8')
        kwargs = {'Bucket': name, 'Key': key, 'Body': body}
        if content_type:
            kwargs['ContentType'] = content_type
        try:
            self.ensure_bucket(name)
            self.get_client().put_object(**kwargs)
        except Exception as exc:
            self._record_upload(key, ok=False, error=str(exc))
            raise
        self._record_upload(key, ok=True)
        return key

    def download_file(self, key, bucket=None):
        """Return the object bytes, raising :class:`S3StorageError` if absent."""
        name = bucket or self.bucket
        client = self.get_client()
        try:
            response = client.get_object(Bucket=name, Key=key)
        except ClientError as exc:
            code = exc.response.get('Error', {}).get('Code')
            if code in ('NoSuchKey', '404'):
                raise S3StorageError(
                    'object not found: {}/{}'.format(name, key)) from exc
            raise S3StorageError(
                'download failed: {}'.format(exc)) from exc
        except Exception as exc:
            raise S3StorageError(
                'storage endpoint unreachable: {}'.format(exc)) from exc
        return response['Body'].read()

    def list_objects(self, bucket=None):
        """Return the raw S3 ``list_objects`` response (route contract)."""
        name = bucket or self.bucket
        self.ensure_bucket(name)
        return self.get_client().list_objects(Bucket=name)

    # -- status panel ------------------------------------------------------

    def _record_upload(self, key, ok, error=None):
        result = {
            'key': key,
            'ok': ok,
            'error': error,
            'at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }
        with self._state_lock:
            self._last_upload = result

    def status(self):
        """Snapshot for the /localstack status panel.

        A live bucket probe is attempted but never raises: if the endpoint
        is unreachable the previous/unknown state is reported alongside the
        probe error, so the panel keeps working while LocalStack is down.
        """
        probe_error = None
        bucket_exists = self._bucket_ready
        try:
            bucket_exists = self.bucket_exists()
        except Exception as exc:
            probe_error = str(exc)
        with self._state_lock:
            last_upload = dict(self._last_upload) if self._last_upload else None
        with self._client_lock:
            client_cached = self._client is not None
        return {
            'client_source': self.client_source,
            'client_cached': client_cached,
            'bucket': self.bucket,
            'bucket_exists': bucket_exists,
            'bucket_probe_error': probe_error,
            'last_upload': last_upload,
        }
