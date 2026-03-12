# app.py
from pathlib import Path
from flask import Flask, url_for, render_template

def create_app():
    app = Flask(__name__)

    # Basis-Konfiguration
    ALLOWED_EXTENSIONS = {'csv'}
    app.config["SECRET_KEY"] = "change-me"  # anpassen
    app.config["DATA_DIR"] = Path(__file__).resolve().parent / "data"
    app.config['UPLOAD_FOLDER'] = Path(__file__).resolve().parent / "data/uploads"
    app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024 
    # Blueprints importieren und registrieren
    from enciclopedia import bp as enciclopedia_bp
    from matching import bp as matching_bp
    from orientation import bp as orientation_bp
    from transform import bp as transform_bp

    app.register_blueprint(enciclopedia_bp, url_prefix="/enciclopedia")
    app.register_blueprint(matching_bp, url_prefix="/matching")
    app.register_blueprint(orientation_bp, url_prefix="/orientation")
    app.register_blueprint(transform_bp, url_prefix="/transform")

    # Main route
    @app.route("/")
    def index():
        return render_template("index.html")


    return app


# Für flask run oder direktes Ausführen
app = create_app()

if __name__ == "__main__":
    app.run(debug=True)
