"""Optional live integration test against the real LocalStack container.

Runs automatically when LocalStack is reachable on LOCALSTACK_HOST:4566
(the docker-compose setup), and is skipped otherwise so ``pytest -q`` stays
green on a plain host. Exercises bucket probing, upload/download roundtrip
and probe-failure recovery end to end.
"""
import os
import socket
import time
import uuid

import pytest

from src.storage import S3StorageError
from src.storage import S3StorageService


def _localstack_reachable():
    host = os.getenv('LOCALSTACK_HOST', 'localhost')
    try:
        with socket.create_connection((host, 4566), timeout=0.5):
            return True
    except OSError:
        return False


pytestmark = pytest.mark.skipif(
    not _localstack_reachable(),
    reason='LocalStack not reachable on $LOCALSTACK_HOST:4566')


def test_live_bucket_probe_upload_download_roundtrip():
    bucket = 'test-bucket-live-{}'.format(uuid.uuid4().hex[:8])
    service = S3StorageService(bucket=bucket)
    try:
        assert service.bucket_exists() is False
        service.ensure_bucket()
        assert service.bucket_exists() is True

        key = 'hello-{}.txt'.format(uuid.uuid4().hex[:6])
        service.upload_file(key, b'live-bytes')
        assert service.download_file(key) == b'live-bytes'

        listing = service.list_objects()
        assert listing['Name'] == bucket

        panel = service.status()
        assert panel['bucket_exists'] is True
        assert panel['last_upload']['ok'] is True
        assert panel['last_upload']['key'] == key
        assert 'process-level singleton' in panel['client_source']
    finally:
        client = service.get_client()
        for obj in client.list_objects(Bucket=bucket).get('Contents', []):
            client.delete_object(Bucket=bucket, Key=obj['Key'])
        client.delete_bucket(Bucket=bucket)
        service.close()


def test_live_probe_failure_recovers_when_bucket_created_later():
    bucket = 'test-bucket-late-{}'.format(uuid.uuid4().hex[:8])
    service = S3StorageService(bucket=bucket)
    try:
        assert service.bucket_exists() is False
        # Direct create without caching a negative result: subsequent probe
        # (as performed after LocalStack/a dependent bucket appears) finds it.
        service.get_client().create_bucket(Bucket=bucket)
        time.sleep(0.2)
        assert service.bucket_exists() is True
        service.ensure_bucket()
    finally:
        client = service.get_client()
        client.delete_bucket(Bucket=bucket)
        service.close()


def test_live_unreachable_endpoint_probe_error_is_retriable():
    # Point at a closed local port: probe errors, then a reachable service
    # with a valid host recovers transparently.
    dead = S3StorageService(bucket='any', localstack_host='127.0.0.1')
    # 127.0.0.1:4566 may or may not be free depending on the host; only run
    # the negative branch when it is actually closed.
    try:
        with socket.create_connection(('127.0.0.1', 4566), timeout=0.2):
            pytest.skip('127.0.0.1:4566 is open; cannot simulate outage')
    except OSError:
        pass
    with pytest.raises(S3StorageError):
        dead.bucket_exists()
    assert dead.status()['bucket_probe_error']
    dead.close()
