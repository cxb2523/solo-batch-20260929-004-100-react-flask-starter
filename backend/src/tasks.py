from celery import Celery

from src.models import Counter
from src.settings import Settings as S
from src.storage import S3StorageService


def make_celery(app):
    # Create and configure the celery app
    celery = Celery(
        app.import_name,
        backend=app.config['CELERY_RESULT_BACKEND'],
        broker=app.config['CELERY_BROKER_URL']
    )
    celery.conf.update(**S.CELERY_CONFIG)

    # Subclass the base Task so that tasks run in a Flask app context
    TaskBase = celery.Task
    class ContextTask(TaskBase):  # noqa
        def __call__(self, *args, **kwargs):  # noqa
            with app.app_context():
                return TaskBase.__call__(self, *args, **kwargs)
    celery.Task = ContextTask

    # Add the tasks
    Tasks.bind(celery)

    return celery


class Tasks:

    # How to add new tasks:
    # - Create the static method (like `ping_once()`)
    # - Add the corresponding bind in the bind() method

    @classmethod
    def bind(cls, celery):
        """Bind the Celery app to all the tasks"""
        # NOTE: DONT
        # Task names are derived from "src.tasks.<function name>"; the
        # decorator preserves the function names/positions, keeping the
        # task-name/argument/return contract stable.
        cls.ping_once = celery.task(cls.ping_once)
        cls.upload_file = celery.task(cls.upload_file)

    # -- storage service injection for async tasks -------------------------
    # Dependency injection rather than a module-level global: the Celery
    # master process imports this module BEFORE prefork. Any boto client
    # built there would be inherited (broken) by forked workers. Keeping
    # the service as a lazily-built per-process slot means each worker
    # process constructs and caches its own S3StorageService (and thus its
    # own connection-pooled client) on first task execution; tests inject a
    # fake service without touching globals.
    _storage = None

    @classmethod
    def set_storage(cls, storage):
        """Override the process storage service (used by tests)."""
        cls._storage = storage

    @classmethod
    def get_storage(cls):
        if cls._storage is None:
            cls._storage = S3StorageService()
        return cls._storage

    @staticmethod
    def ping_once(amount):
        for counter in Counter.list():
            counter.increment(amount=amount)

    @staticmethod
    def upload_file(key, body):
        """Upload one object to S3 from inside a worker process.

        Returns a plain dict ``{'key': ..., 'bucket': ...}`` (Celery result
        backend needs JSON-serializable values).
        """
        storage = Tasks.get_storage()
        storage.upload_file(key, body)
        return {'key': key, 'bucket': storage.bucket}
