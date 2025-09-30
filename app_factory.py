from flask import Flask
from enciclopedia_app import enciclopedia_bp
from orientation_app import orientation_bp
from matching_app import matching_bp

def create_app():
    app = Flask(
        __name__,
        static_url_path='/static',
        static_folder='static',
        template_folder=None  # we'll let blueprints manage their templates
    )

    app.config['DATA_PATH'] = 'data'

    # Register blueprints with different URL prefixes
    app.register_blueprint(enciclopedia_bp, url_prefix='/enciclopedia')
    app.register_blueprint(orientation_bp, url_prefix='/orientation')
    app.register_blueprint(matching_bp, url_prefix='/matching')

    return app
