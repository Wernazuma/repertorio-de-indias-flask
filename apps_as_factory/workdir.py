"""Single source of truth for the ephemeral working directory.

User uploads and every derived file are pure scratch: the authoritative copy lives
in the private ARCA-data GitHub repo (see enciclopedia/data_sink.py), and the server
keeps files only while it is actively processing them. So the working dir is an
OS-temp location OUTSIDE the repo, disposable at any time (a restart just re-hydrates
from the stash) — never the tracked data/uploads folder.

Override with the ARCA_WORK_DIR env var (e.g. a tmpfs mount) if desired.
"""
import os
import tempfile

WORK_UPLOADS = (os.environ.get("ARCA_WORK_DIR")
                or os.path.join(tempfile.gettempdir(), "arca_work", "uploads"))


def ensure():
    """Create the working dir (idempotent). Returns the path."""
    os.makedirs(WORK_UPLOADS, exist_ok=True)
    return WORK_UPLOADS
