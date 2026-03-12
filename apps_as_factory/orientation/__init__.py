from flask import Blueprint

bp = Blueprint(
    "orientation",
    __name__,
    template_folder="templates",
    static_folder="static",
)

from . import routes  # noqa: F401
