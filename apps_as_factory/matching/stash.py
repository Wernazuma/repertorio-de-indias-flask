"""Durable, GitHub-backed working state for one matching run.

The deployment has no persistent disk, and the working dir is ephemeral OS-temp scratch
(see workdir.py), so a run's files can't be trusted to survive between sittings. This
module packs that per-run file set into a single zip stashed on the private data repo
(see enciclopedia/data_sink.py), keyed by the resume code "<prefix>~<token>", and
hydrates it back on resume.

Commit policy: the server keeps a transient working copy on disk during an active
sitting; a checkpoint (one commit) happens only at stage boundaries and on an explicit
"pause" — never per disambiguation click. Resume only matters after normalization, so
the first stash is written when the cleaned table is produced.
"""
import os
import secrets

from flask import current_app

from enciclopedia import data_sink

# The per-run working files worth carrying between sittings (exact suffixes; a
# prefix like "Tlaxcala" never matches "Tlaxcala_1"). Mirrors _DATASET_SUFFIXES in
# routes.py, minus transient progress (_status.*) and regenerated export outputs
# (_results.csv / _original_results.csv / .geojson), plus the resume-token sidecar.
STASH_SUFFIXES = (
    ".csv", "_formatted.csv", "_cleaned.csv", "_processing.csv",
    "_domain.txt", "_metadata.txt", "_bibliography.txt", "_field_definitions.csv",
    "_constraints.json",
    "_phase1a_complete.flag", "_phase1b_complete.flag",
    "_reviewed.flag", "_undo.csv", "_undo.json",
    "_territory_results.csv",
    "_token.txt",
)


def _folder():
    return str(current_app.config['UPLOAD_FOLDER'])


# The resume code is "<prefix>~<token>": self-describing (the user sees the run) and
# the key into workspace/<prefix>/<token>. "~" is absent from both a secure_filename
# prefix and a token_urlsafe token, so the split is unambiguous.
def code_path(prefix):
    return os.path.join(_folder(), f"{prefix}_token.txt")


def get_code(prefix):
    try:
        with open(code_path(prefix), encoding='utf-8') as fh:
            return fh.read().strip()
    except OSError:
        return ''


def set_code(prefix, code):
    os.makedirs(_folder(), exist_ok=True)
    with open(code_path(prefix), 'w', encoding='utf-8') as fh:
        fh.write(code)


def new_code(prefix):
    return f"{prefix}~{secrets.token_urlsafe(9)}"


def parse_code(code):
    """Split a resume code into (prefix, token), both validated. Returns
    (None, None) when the code is malformed."""
    if not code or '~' not in str(code):
        return (None, None)
    prefix, _, token = str(code).partition('~')
    if data_sink.valid_prefix(prefix) and data_sink.valid_token(token):
        return (prefix, token)
    return (None, None)


def enabled():
    """True when durable stashing is available (data repo configured)."""
    return data_sink.configured()


def pack(prefix):
    folder = _folder()
    pairs = [(os.path.join(folder, f"{prefix}{suf}"), f"{prefix}{suf}")
             for suf in STASH_SUFFIXES]
    return data_sink.pack_files(pairs)


def unpack(prefix, zip_bytes):
    return data_sink.unpack_to(zip_bytes, _folder())


def checkpoint(prefix):
    """Write the current working state to the stash (one commit). No-op when the
    data repo isn't configured or the run has no code yet."""
    prefix2, token = parse_code(get_code(prefix))
    if not (token and prefix2 == prefix and enabled()):
        return False
    return data_sink.stash_save(prefix, token, pack(prefix))


def start(prefix):
    """Mint a resume code for a run and write its first stash. Returns the full
    "<prefix>~<token>" code (or '' when durable stashing is unavailable)."""
    if not enabled():
        return ''
    code = new_code(prefix)
    set_code(prefix, code)
    checkpoint(prefix)
    return code


def hydrate(code):
    """Fetch a stash by resume code and unpack it into the upload folder. Returns
    the run's prefix on success, or None when the code is unknown/unavailable."""
    prefix, token = parse_code(code)
    if not (prefix and token and enabled()):
        return None
    zip_bytes, _meta = data_sink.stash_load(prefix, token)
    if not zip_bytes:
        return None
    unpack(prefix, zip_bytes)
    set_code(prefix, code)
    return prefix


def finish(prefix):
    """Delete the workspace stash once a run is completed/abandoned."""
    prefix2, token = parse_code(get_code(prefix))
    if token and prefix2 == prefix and enabled():
        data_sink.stash_delete(prefix, token)
