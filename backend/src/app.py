from flask import Flask
from flask_migrate import Migrate

from src import routes, utils as u
from src.models import db
from src.settings import Settings as S
from src.tasks import make_celery


class Application:

    @classmethod
    def boot(cls):
        """Begin the application"""
        app = cls()
        app.run()
        return app

    def __init__(self):
        self.init_flask()
        self.init_routes()

    def init_flask(self):
        # Init Flask
        self.flask_app = Flask(__name__, static_url_path='/static')
        self.flask_app.config.from_object(S)

        # Register the public routes
        for blueprint in [routes.counter, routes.localstack]:
            self.flask_app.register_blueprint(blueprint)

        # Register the injectable storage service used by the /localstack
        # routes (routes consume it via app.extensions, never a global).
        routes.localstack.init_storage(self.flask_app)

        # Init Celery
        self.celery = make_celery(self.flask_app)

        # Init Flask-SQLAlchemy
        self.db = db
        self.db.init_app(self.flask_app)

        # Init Flask-Migrate
        u.wait_for_service('postgres', 5432, timeout=30.0)
        self.migrate = Migrate(self.flask_app, self.db)

    def init_routes(self):

        @self.flask_app.route('/')
        def home(key):
            return 'home'
