"""Contract tests for the /localstack (S3) routes.

The URL paths, HTTP methods and response shapes must match the pre-refactor
behavior; only the internal boto client construction moves behind the
injected storage service.
"""
import io

import pytest
from flask import Flask

from src.routes import localstack


class FakeStorage:
    def __init__(self, bucket='test-bucket', existing_bucket=True):
        self.bucket = bucket
        self.existing_bucket = existing_bucket
        self.uploads = []
        self.raise_on_upload = False

    def list_objects(self, bucket=None):
        return {'Name': bucket or self.bucket,
                'Contents': [{'Key': 'upload-x.png'}]}

    def upload_file(self, key, body, bucket=None, **kwargs):
        if self.raise_on_upload:
            from src.storage import S3StorageError
            raise S3StorageError('endpoint down')
        if hasattr(body, 'read'):
            body = body.read()
        self.uploads.append((key, body))
        return key

    def download_file(self, key, bucket=None):
        return b'downloaded'

    def status(self):
        return {
            'client_source': 'injected-fake',
            'client_cached': True,
            'bucket': self.bucket,
            'bucket_exists': self.existing_bucket,
            'bucket_probe_error': None,
            'last_upload': {'key': self.uploads[-1][0], 'ok': True,
                            'error': None, 'at': 'now'} if self.uploads
            else None,
        }


@pytest.fixture
def client():
    app = Flask(__name__)
    storage = FakeStorage()
    localstack.init_storage(app, storage=storage)
    app.register_blueprint(localstack.blueprint)
    app.storage = storage
    with app.test_client() as test_client:
        yield test_client, storage


def test_files_route_contract(client):
    test_client, _ = client
    resp = test_client.get('/api/v1/files/')
    assert resp.status_code == 200
    body = resp.get_json()
    assert body['Name'] == 'test-bucket'
    assert body['Contents'][0]['Key'] == 'upload-x.png'


def test_upload_get_is_unchanged(client):
    test_client, _ = client
    # GET never had a defined body and should remain a 200 (template-less).
    resp = test_client.get('/api/v1/files/upload/')
    assert resp.status_code == 200


def test_upload_post_no_file_message_contract(client):
    test_client, _ = client
    resp = test_client.post('/api/v1/files/upload/')
    assert resp.status_code == 200
    assert resp.get_json() == {'message': 'no file'}


def test_upload_post_returns_message_filename(client):
    test_client, storage = client
    data = {'file': (io.BytesIO(b'img-bytes'), 'photo.PNG')}
    resp = test_client.post('/api/v1/files/upload/',
                            data=data,
                            content_type='multipart/form-data')
    assert resp.status_code == 200
    message = resp.get_json()['message']
    assert message.startswith('upload-')
    assert message.endswith('.PNG')
    assert storage.uploads[0][0] == message
    assert storage.uploads[0][1] == b'img-bytes'


def test_upload_storage_failure_propagates(client):
    test_client, storage = client
    storage.raise_on_upload = True
    data = {'file': (io.BytesIO(b'x'), 'a.png')}
    with pytest.raises(Exception):
        test_client.post('/api/v1/files/upload/',
                         data=data,
                         content_type='multipart/form-data')


def test_create_route_redirects_to_files(client):
    test_client, storage = client
    resp = test_client.get('/api/v1/files/create/')
    assert resp.status_code == 302
    assert resp.headers['Location'].endswith('/api/v1/files/')
    assert len(storage.uploads) == 1
    assert storage.uploads[0][0].endswith('.txt')
    assert storage.uploads[0][1] == b'some content'


def test_status_route_exposes_panel_payload(client):
    test_client, storage = client
    resp = test_client.get('/api/v1/files/status/')
    assert resp.status_code == 200
    body = resp.get_json()
    assert body['client_source'] == 'injected-fake'
    assert body['bucket'] == 'test-bucket'
    assert body['bucket_exists'] is True
    assert body['last_upload'] is None


def test_injection_is_per_app_and_clients_do_not_cross():
    """Two apps with different storage services must not share state."""
    app_a, app_b = Flask(__name__), Flask(__name__)
    storage_a, storage_b = FakeStorage(), FakeStorage()
    localstack.init_storage(app_a, storage=storage_a)
    localstack.init_storage(app_b, storage=storage_b)
    app_a.register_blueprint(localstack.blueprint)
    app_b.register_blueprint(localstack.blueprint)

    client_a = app_a.test_client()
    client_b = app_b.test_client()
    data = {'file': (io.BytesIO(b'from-a'), 'a.png')}
    client_a.post('/api/v1/files/upload/', data=data,
                  content_type='multipart/form-data')

    assert len(storage_a.uploads) == 1
    assert storage_b.uploads == []
    status_b = client_b.get('/api/v1/files/status/').get_json()
    assert status_b['last_upload'] is None
