# enciclopedia/__init__.py
from flask import Blueprint

# Name "enciclopedia" ist der Blueprint-Name
bp = Blueprint(
    "enciclopedia",
    __name__,
    template_folder="templates",
    static_folder="static",
)

# die Routen in routes.py registrieren
from . import routes  # noqa: F401
