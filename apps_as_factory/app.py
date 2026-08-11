# app.py
import os
import json
import secrets
from datetime import datetime
from pathlib import Path
from flask import Flask, url_for, render_template, abort
from werkzeug.middleware.proxy_fix import ProxyFix

from i18n import init_app as init_i18n


def _load_local_secrets():
    """Read secrets (ADMIN_TOKEN, SECRET_KEY) from an untracked local_secrets.json
    next to this file, if present. Values here take precedence over env vars."""
    p = Path(__file__).resolve().parent / "local_secrets.json"
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            pass
    return {}

# Subpages of the Atlas / HGIS de las Indias section. Each renders through
# pages/atlas_sub.html until it gets its own content.
ATLAS_PAGES = {
    # The web apps (Explorative GIS, Narrative Maps, Guided Tours) now live as
    # sections on the /atlas page itself rather than on separate subpages.
    "technical-methodology": {
        "title": "Technical Methodology",
        "eyebrow": "Atlas · Methodology",
        "lead": "Data model, gazetteer structure, geometries, and the technical "
                "decisions behind HGIS de las Indias.",
        "template": "pages/atlas_technical.html",
    },
    "historical-methodology": {
        "title": "Historical Methodology",
        "eyebrow": "Atlas · Methodology",
        "lead": "Sources, criteria, and historiographical principles used to "
                "reconstruct places and jurisdictions.",
        "template": "pages/atlas_historical.html",
    },
    "project-history": {
        "title": "Project History",
        "eyebrow": "Atlas · History",
        "lead": "From the FWF-funded project at the University of Graz to the ARCA framework.",
        "template": "pages/atlas_history.html",
    },
    "publications": {
        "title": "Publications",
        "eyebrow": "Atlas · Publications",
        "lead": "Articles, datasets, and presentations documenting and building on "
                "HGIS de las Indias.",
        "template": "pages/atlas_publications.html",
    },
}

def create_app():
    app = Flask(__name__)
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_host=1, x_proto=1, x_prefix=1)

    # Basis-Konfiguration
    ALLOWED_EXTENSIONS = {'csv'}
    # Secrets come from local_secrets.json (untracked) first, then env vars.
    _secrets = _load_local_secrets()
    # Signs the suggestion-form anti-bot tokens and Flask sessions. If no secret
    # is configured we generate a random ephemeral one rather than fall back to a
    # public constant (a known key lets anyone forge anti-bot tokens / sessions).
    # The only cost of the random fallback: a restart invalidates outstanding
    # sessions and challenge tokens — harmless. Set SECRET_KEY in local_secrets.json
    # (or ARCA_SECRET_KEY) for a stable key that survives restarts.
    app.config["SECRET_KEY"] = (_secrets.get("SECRET_KEY")
                                or os.environ.get("ARCA_SECRET_KEY")
                                or secrets.token_hex(32))
    # Token that gates the suggestions admin page (unset -> admin page disabled).
    app.config["ADMIN_TOKEN"] = (_secrets.get("ADMIN_TOKEN")
                                 or os.environ.get("ARCA_ADMIN_TOKEN")
                                 or "")
    app.config["DATA_DIR"] = Path(__file__).resolve().parent / "data"
    app.config['UPLOAD_FOLDER'] = Path(__file__).resolve().parent / "data/uploads"
    app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024
    # Session cookie hardening. HttpOnly is Flask's default; SameSite=Lax stops the
    # admin session cookie riding along on cross-site requests (defence in depth for
    # the admin login/clear flow). Set SESSION_COOKIE_SECURE=True once served over
    # HTTPS (leaving it off here so the cookie works on the plain-HTTP test server).
    app.config['SESSION_COOKIE_HTTPONLY'] = True
    app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
    # Blueprints importieren und registrieren
    from enciclopedia import bp as enciclopedia_bp
    from matching import bp as matching_bp
    from orientation import bp as orientation_bp
    from transform import bp as transform_bp

    app.register_blueprint(enciclopedia_bp, url_prefix="/apps/enciclopedia")
    app.register_blueprint(matching_bp, url_prefix="/matching")
    app.register_blueprint(orientation_bp, url_prefix="/orientation")
    app.register_blueprint(transform_bp, url_prefix="/transform")

    # Bilingual (EN/ES) support: language toggle route + t / vt / lang in templates.
    init_i18n(app)

    # ------------------------------------------------------------------
    # Site-wide template globals (default theme, external Atlas URL, year).
    # The active theme is chosen client-side by the switcher and persisted in
    # localStorage; this only sets the server-rendered default.
    # ------------------------------------------------------------------
    @app.context_processor
    def inject_site_globals():
        return {
            "theme": "slate",
            "atlas_url": "https://www.hgis-indias.net/index.php/en/webgis",
            "year": datetime.now().year,
        }

    # ---- ARCA static/site pages ----
    @app.route("/")
    def index():
        # Welcome landing page.
        return render_template("pages/home.html")

    @app.route("/arca")
    def arca_hub():
        # Geodata-workflow hub (replaces the old "Geodata Workflow" index).
        return render_template("pages/arca_hub.html")

    @app.route("/atlas")
    def atlas():
        # In-site HGIS de las Indias / Atlas landing.
        return render_template("pages/atlas.html")

    @app.route("/atlas/<slug>")
    def atlas_page(slug):
        page = ATLAS_PAGES.get(slug)
        if page is None:
            abort(404)
        # a page may supply its own template; otherwise the shared placeholder
        return render_template(
            page.get("template", "pages/atlas_sub.html"), page=page, slug=slug
        )

    @app.route("/news")
    def news():
        return render_template("pages/news.html")

    @app.route("/datasets")
    def datasets():
        return render_template("pages/datasets.html")

    @app.route("/collaborate")
    def collaborate():
        return render_template("pages/collaborate.html")

    @app.route("/bring-your-data")
    def bring_your_data():
        # Entry point to the geodata workflow: guide / upload / match / resume.
        return render_template("pages/bring_your_data.html")

    @app.route("/team")
    def team():
        return render_template("pages/team.html")

    @app.route("/impressum")
    def impressum():
        return render_template("pages/impressum.html")

    return app


# Für flask run oder direktes Ausführen
app = create_app()

if __name__ == "__main__":
    # threaded=True so the live progress page can be polled while the
    # matching pipeline runs in a background thread.
    # Debug (interactive debugger + auto-reload) is OFF unless ARCA_DEBUG=1 is set
    # in the environment — the run_dev.bat launcher sets it. Never enable it on a
    # reachable host: the debugger console is a remote code-execution surface.
    debug = os.environ.get("ARCA_DEBUG") == "1"
    app.run(debug=debug, threaded=True)
