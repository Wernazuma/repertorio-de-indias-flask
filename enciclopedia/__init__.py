from flask import Blueprint

enciclopedia_bp = Blueprint(
    'enciclopedia', __name__,
    template_folder='templates',
    static_folder=None  # We'll use global static
)

from . import views  # Make sure views.py registers routes
