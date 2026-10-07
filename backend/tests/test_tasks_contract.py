"""Contract tests for the Celery tasks.

Guards:
* task names remain ``src.tasks.ping_once`` / ``src.tasks.upload_file``;
* arguments and return shapes are unchanged (ping_once -> None,
  upload_file -> {'key', 'bucket'});
* storage access is injected per process, and concurrent task invocations
  with different injected storages never cross.
"""
from celery import Celery
import pytest

from src.storage import S3StorageService
from src.tasks import Tasks


@pytest.fixture
def celery_app():
    app = Celery('test', broker='memory://', backend='cache+memory://')
    app.conf.update(task_always_eager=True)
    Tasks.bind(app)
    return app


@pytest.fixture(autouse=True)
def reset_storage():
    Tasks.set_storage(None)
    yield
    Tasks.set_storage(None)


class FakeCounter:
    incremented = []

    def increment(self, amount=1):
        self.incremented.append(amount)


class FakeStorage:
    def __init__(self, name):
        self.bucket = 'bucket-{}'.format(name)
        self.uploads = []

    def upload_file(self, key, body):
        self.uploads.append((key, body))
        return key


def test_ping_once_task_name_contract(celery_app):
    assert Tasks.ping_once.name == 'src.tasks.ping_once'


def test_upload_file_task_name_contract(celery_app):
    assert Tasks.upload_file.name == 'src.tasks.upload_file'


def test_ping_once_argument_and_return_contract(celery_app, monkeypatch):
    FakeCounter.incremented = []
    monkeypatch.setattr('src.tasks.Counter.list',
                        classmethod(lambda cls: [FakeCounter()]))
    result = Tasks.ping_once.apply(args=(3,)).get()
    assert result is None
    assert FakeCounter.incremented == [3]


def test_upload_file_argument_and_return_contract(celery_app):
    storage = FakeStorage('x')
    Tasks.set_storage(storage)
    result = Tasks.upload_file.apply(args=('k.txt', b'bytes')).get()
    assert result == {'key': 'k.txt', 'bucket': 'bucket-x'}
    assert storage.uploads == [('k.txt', b'bytes')]


def test_storage_is_injected_not_global_and_lazily_built():
    # Nothing built until requested (Celery master must not construct boto).
    Tasks.set_storage(None)
    storage = Tasks.get_storage()
    assert isinstance(storage, S3StorageService)
    # Same per-process singleton on repeated access.
    assert Tasks.get_storage() is storage


def test_concurrent_tasks_do_not_cross_storage_clients():
    """Two storages injected in sequence must not see each other's calls."""
    storage_a = FakeStorage('a')
    storage_b = FakeStorage('b')

    Tasks.set_storage(storage_a)
    Tasks.upload_file.apply(args=('a-key', b'a-body')).get()
    Tasks.set_storage(storage_b)
    Tasks.upload_file.apply(args=('b-key', b'b-body')).get()

    assert storage_a.uploads == [('a-key', b'a-body')]
    assert storage_b.uploads == [('b-key', b'b-body')]
