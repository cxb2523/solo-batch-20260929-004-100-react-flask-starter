import uuid
import time

from flask import Blueprint
from flask import current_app
from flask import jsonify
from flask import redirect
from flask import request
from flask import url_for
from werkzeug.utils import secure_filename

from src.storage import S3StorageError
from src.storage import S3StorageService


BUCKET = 'test-bucket'

blueprint = Blueprint('aws', __name__)

STORAGE_EXTENSION_KEY = 's3_storage'


def init_storage(app, storage=None):
    """Attach the injectable storage service to the Flask app.

    Dependency-injection seam: the routes never import a global service.
    Tests (or other app factories) can pass any object implementing
    upload_file/download_file/list_objects/status, and nothing reaches out
    to LocalStack until first use - important because the Celery worker
    imports app modules in its master process before fork.
    """
    app.extensions[STORAGE_EXTENSION_KEY] = (
        storage if storage is not None else S3StorageService(bucket=BUCKET))


def get_storage():
    # current_app proxies the exact instance registered via init_storage,
    # so concurrent requests each see the same injected service while the
    # service's own client cache is thread-safe internally.
    return current_app.extensions[STORAGE_EXTENSION_KEY]


@blueprint.route('/api/v1/files/')
def files():
    return jsonify(get_storage().list_objects())


@blueprint.route('/api/v1/files/upload/', methods=['GET', 'POST'])
def upload():
    if request.method == 'POST':
        if 'file' not in request.files:
            return jsonify({'message': 'no file'})
        file = request.files['file']
        if file:
            timestr = time.strftime('%Y%m%d%H%M%S')
            ext = secure_filename(file.filename).split('.')[-1]
            filename = f'upload-{timestr}.{ext}'
            try:
                get_storage().upload_file(filename, file)
            except S3StorageError:
                # Pre-refactor behavior surfaced the exception to Flask's
                # error handler; keep that contract instead of masking it.
                raise
            return jsonify(message=filename)


@blueprint.route('/api/v1/files/create/')
def add_file():
    get_storage().upload_file('{}.txt'.format(uuid.uuid1()),
                              b'some content')
    return redirect(url_for('.files'))


@blueprint.route('/api/v1/files/status/')
def status():
    """Status panel data: client origin, bucket presence, last upload."""
    return jsonify(get_storage().status())
