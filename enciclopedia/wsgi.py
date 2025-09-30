import sys
import os

# Add the project directory to the sys.path
sys.path.insert(0, '/var/www/enciclopedia')

# Activate the virtual environment
activate_this = '/var/www/enciclopedia/venv/bin/activate_this.py'
with open(activate_this) as file_:
    exec(file_.read(), dict(__file__=activate_this))

# Import the Flask app
from enciclopedia import app as application
