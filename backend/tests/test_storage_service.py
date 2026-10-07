"""Tests for the injectable S3 storage service.

Covers: process-level singleton client cache + thread safety,
concurrent callers must not build/leak separate clients
("concurrent calls don't cross/leak clients"), and recovery after a failed
client construction or bucket probe.
"""
import threading

import pytest
from botocore.exceptions import ClientError as BotoClientError

from src.storage import S3StorageError
from src.storage import S3StorageService


class FakeEndpointError(Exception):
    """Stands in for a botocore connection-level error."""


def client_error(code, status, operation='HeadBucket'):
    return BotoClientError(
        {'Error': {'Code': code},
         'ResponseMetadata': {'HTTPStatusCode': status}},
        operation)


class FakeS3:
    """Minimal S3 client double with scriptable behavior."""

    def __init__(self,
                 buckets=None,
                 head_error=None,
                 create_error=None,
                 put_error=None,
                 objects=None):
        self.buckets = buckets if buckets is not None else set()
        self.head_error = head_error
        self.create_error = create_error
        self.put_error = put_error
        self.objects = objects if objects is not None else {}
        self.calls = []
        self.closed = False

    def head_bucket(self, Bucket):
        self.calls.append(('head_bucket', Bucket))
        if self.head_error is not None:
            raise self.head_error
        if Bucket not in self.buckets:
            raise client_error('404', 404)

    def create_bucket(self, Bucket):
        self.calls.append(('create_bucket', Bucket))
        if self.create_error is not None:
            raise self.create_error
        self.buckets.add(Bucket)

    def put_object(self, Bucket, Key, Body, **kwargs):
        self.calls.append(('put_object', (Bucket, Key)))
        if self.put_error is not None:
            raise self.put_error
        if isinstance(Body, str):
            Body = Body.encode('utf-8')
        elif hasattr(Body, 'read'):
            Body = Body.read()
        self.objects[(Bucket, Key)] = Body

    def get_object(self, Bucket, Key):
        self.calls.append(('get_object', (Bucket, Key)))
        if (Bucket, Key) not in self.objects:
            raise client_error('NoSuchKey', 404, operation='GetObject')

        class _Body:
            def __init__(self, data):
                self._data = data

            def read(self):
                return self._data

        return {'Body': _Body(self.objects[(Bucket, Key)])}

    def list_objects(self, Bucket):
        self.calls.append(('list_objects', Bucket))
        return {
            'Name': Bucket,
            'Contents': [{'Key': key} for _, key
                         in sorted((b, k) for (b, k) in self.objects
                                   if b == Bucket)],
        }

    def close(self):
        self.closed = True


def make_service(**client_kwargs):
    holder = {}

    def factory():
        holder['client'] = FakeS3(**client_kwargs)
        return holder['client']

    service = S3StorageService(bucket='bkt', client_factory=factory,
                               client_source='test-factory')
    return service, holder


def test_client_is_process_level_cached_singleton():
    service, holder = make_service()
    assert service.get_client() is service.get_client()
    assert service.status()['client_cached'] is True


def test_concurrent_callers_share_one_client_and_factory_runs_once():
    """Concurrency must not build separate clients (no client crossing)."""
    built = []
    started = threading.Event()

    def factory():
        # Hold the construction window open while the other threads enter
        # get_client() and block on the service lock - a barrier here would
        # deadlock since the factory runs while the lock is held.
        started.set()
        threading.Event().wait(0.2)
        client = FakeS3()
        built.append(client)
        return client

    service = S3StorageService(bucket='bkt', client_factory=factory)
    results = []

    def grab():
        results.append(service.get_client())

    def grab_waiting():
        started.wait()
        results.append(service.get_client())

    threads = [threading.Thread(target=grab)]
    threads += [threading.Thread(target=grab_waiting) for _ in range(7)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(built) == 1
    assert results == [built[0]] * 8


def test_injected_services_do_not_share_clients():
    """Two injected service instances (e.g. per app/test) stay isolated."""
    service_a, holder_a = make_service()
    service_b, holder_b = make_service()
    assert service_a.get_client() is not service_b.get_client()
    service_a.ensure_bucket()
    service_b.ensure_bucket()
    assert holder_a['client'] is not holder_b['client']


def test_client_factory_failure_is_not_cached_and_retries():
    """Recovery: a failing first construction must succeed on the next call."""
    calls = {'n': 0}

    def flaky_factory():
        calls['n'] += 1
        if calls['n'] == 1:
            raise FakeEndpointError('localstack not up yet')
        return FakeS3(buckets={'bkt'})

    service = S3StorageService(bucket='bkt', client_factory=flaky_factory)
    with pytest.raises(FakeEndpointError):
        service.get_client()
    assert service.status()['client_cached'] is False

    client = service.get_client()  # recovered after endpoint came back
    assert isinstance(client, FakeS3)
    assert calls['n'] == 2


def test_ensure_bucket_creates_missing_bucket_then_skips_probe():
    service, holder = make_service()
    service.ensure_bucket()
    service.ensure_bucket()
    client = holder['client']
    assert ('create_bucket', 'bkt') in client.calls
    # head once for the initial probe; the second ensure uses the cache.
    assert client.calls.count(('head_bucket', 'bkt')) == 1


def test_probe_failure_is_not_cached_and_recovers():
    """Recovery: endpoint down during probe -> retry later -> ready."""
    service, holder = make_service(head_error=FakeEndpointError('boom'))
    with pytest.raises(S3StorageError):
        service.ensure_bucket()

    # Negative probe must not be cached: client rebuilt? No - the client
    # itself is fine; the probe is re-issued and now reports the bucket.
    holder['client'].head_error = None
    holder['client'].buckets.add('bkt')
    service.ensure_bucket()
    assert service.status()['bucket_exists'] is True


def test_bucket_exists_false_for_missing_bucket():
    service, holder = make_service()
    assert service.bucket_exists() is False


def test_upload_records_success_and_ensures_bucket():
    service, holder = make_service()
    key = service.upload_file('a.txt', b'hello')
    assert key == 'a.txt'
    client = holder['client']
    assert client.objects[('bkt', 'a.txt')] == b'hello'
    status = service.status()
    assert status['last_upload']['ok'] is True
    assert status['last_upload']['key'] == 'a.txt'
    assert status['bucket_exists'] is True


def test_upload_records_failure_and_raises():
    service, holder = make_service(buckets={'bkt'},
                                   put_error=FakeEndpointError('reset'))
    with pytest.raises(FakeEndpointError):
        service.upload_file('a.txt', b'hello')
    status = service.status()
    assert status['last_upload']['ok'] is False
    assert 'reset' in status['last_upload']['error']


def test_download_roundtrip_and_missing_object_error():
    service, holder = make_service(buckets={'bkt'},
                                   objects={('bkt', 'a'): b'data'})
    assert service.download_file('a') == b'data'
    with pytest.raises(S3StorageError):
        service.download_file('missing')


def test_status_reports_probe_error_without_raising():
    service, holder = make_service(head_error=FakeEndpointError('nope'))
    status = service.status()
    assert status['bucket_exists'] is False
    assert 'nope' in status['bucket_probe_error']
    assert status['client_source'] == 'test-factory'
    assert status['bucket'] == 'bkt'


def test_close_drops_client_and_bucket_cache():
    service, holder = make_service()
    service.ensure_bucket()
    service.close()
    assert holder['client'].closed is True
    assert service.status()['client_cached'] is False
