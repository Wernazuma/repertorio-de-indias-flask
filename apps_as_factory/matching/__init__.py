from flask import Blueprint

# Create the Blueprint once, no routes here
bp = Blueprint(
    "matching",
    __name__,
    template_folder="templates",
    static_folder="static",
)

# Import routes so they attach to this Blueprint
from . import routes  # noqa: F401
