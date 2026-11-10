"""Durable storage for the upload/matching pipeline on a second private GitHub repo.

The deployment has no persistent disk, so a matching run's working state can't live
on the server between sittings. GitHub is the source of truth instead:

  /workspace/<prefix>/<token>.zip   in-progress run (all its working files), + a
  /workspace/<prefix>/<token>.json  meta {prefix, token, created, updated}. 30-day TTL.
  /datasets/<prefix>/<stamp>...     permanently published, released result bundles.
  /usage-log/<prefix>/<stamp>.json  permanent registry of who downloaded a bundle & why.

Every area is keyed by <prefix>, so one run's workspace, dataset and usage records
line up under the same <prefix>/ folder.

Config (app.config, from local_secrets.json / env):
  GITHUB_DATA_REPO   "owner/name" of the private data repo (separate from suggestions).
  GITHUB_DATA_TOKEN  fine-grained PAT with Contents: Read/Write on that repo. Falls
                     back to GITHUB_TOKEN when one PAT is scoped to both repos.

Everything fails soft: on a network/API error the helpers return None/False/[], and
callers surface a "try again" state — nothing is silently lost, because the server's
transient working copy is still there for the current sitting.
"""
import csv
import io
import json
import re
import zipfile
from datetime import datetime, timezone

from flask import current_app

from . import github_sink

# A resume token minted by us (secrets.token_urlsafe) is [A-Za-z0-9_-]; the run's
# prefix (secure_filename stem) is [A-Za-z0-9._-]. Both are validated before they
# reach a repo path (path-traversal guard on resume). Every ARCA-data area is keyed
# by <prefix>, so workspace/datasets/usage-log for one run share a <prefix>/ folder.
TOKEN_RE = re.compile(r'^[A-Za-z0-9_-]{8,64}$')
PREFIX_RE = re.compile(r'^[A-Za-z0-9._-]{1,120}$')

WORKSPACE_DIR = 'workspace'
DATASETS_DIR = 'datasets'
USAGE_DIR = 'usage-log'
STASH_TTL_DAYS = 30


def _repo():
    return current_app.config.get('GITHUB_DATA_REPO', '')


def _token():
    return (current_app.config.get('GITHUB_DATA_TOKEN', '')
            or current_app.config.get('GITHUB_TOKEN', ''))


def configured():
    return bool(_repo() and _token())


def valid_token(token_id):
    return bool(token_id and TOKEN_RE.match(token_id))


def valid_prefix(prefix):
    return bool(prefix and PREFIX_RE.match(prefix) and '..' not in prefix)


def _now_iso():
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _stamp():
    return datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')


# ---------------------------------------------------------------------------
# Workspace stash (in-progress runs)
# ---------------------------------------------------------------------------

def _ws_zip(prefix, token_id):
    return '%s/%s/%s.zip' % (WORKSPACE_DIR, prefix, token_id)


def _ws_meta(prefix, token_id):
    return '%s/%s/%s.json' % (WORKSPACE_DIR, prefix, token_id)


def stash_save(prefix, token_id, zip_bytes, created=None):
    """Create/overwrite a run's stash zip + meta under workspace/<prefix>/. Returns
    True on success. Also lazily prunes stashes older than the TTL."""
    if not (configured() and valid_prefix(prefix) and valid_token(token_id)):
        return False
    repo, token = _repo(), _token()
    if not created:
        prev_raw, _psha = github_sink.get_file(_ws_meta(prefix, token_id), repo, token)
        if prev_raw:
            try:
                created = json.loads(prev_raw.decode('utf-8')).get('created')
            except ValueError:
                created = None
    meta = {'prefix': prefix, 'token': token_id,
            'created': created or _now_iso(), 'updated': _now_iso()}
    ok_zip = github_sink.put_file_upsert(
        _ws_zip(prefix, token_id), zip_bytes, 'workspace: %s' % prefix, repo, token)
    ok_meta = github_sink.put_file_upsert(
        _ws_meta(prefix, token_id), json.dumps(meta).encode('utf-8'),
        'workspace meta: %s' % prefix, repo, token)
    try:
        prune_expired()
    except Exception:
        pass
    return bool(ok_zip and ok_meta)


def stash_load(prefix, token_id):
    """Fetch a run's stash. Returns (zip_bytes, meta_dict) or (None, None)."""
    if not (configured() and valid_prefix(prefix) and valid_token(token_id)):
        return (None, None)
    repo, token = _repo(), _token()
    raw, _sha = github_sink.get_file(_ws_zip(prefix, token_id), repo, token)
    if raw is None:
        return (None, None)
    meta_raw, _msha = github_sink.get_file(_ws_meta(prefix, token_id), repo, token)
    meta = {}
    if meta_raw:
        try:
            meta = json.loads(meta_raw.decode('utf-8'))
        except ValueError:
            meta = {}
    return (raw, meta)


def stash_delete(prefix, token_id):
    """Remove a run's stash zip + meta (run finished or abandoned)."""
    if not (configured() and valid_prefix(prefix) and valid_token(token_id)):
        return False
    repo, token = _repo(), _token()
    a = github_sink.delete_file(_ws_zip(prefix, token_id), 'workspace done: %s' % prefix, repo, token)
    github_sink.delete_file(_ws_meta(prefix, token_id), 'workspace meta done: %s' % prefix, repo, token)
    return bool(a)


def prune_expired(days=STASH_TTL_DAYS):
    """Delete workspace stashes whose meta.updated is older than the TTL. Walks
    workspace/<prefix>/*.json (the top-level README.md and non-dir entries are
    skipped)."""
    if not configured():
        return 0
    repo, token = _repo(), _token()
    removed = 0
    for pfx in github_sink.list_dir(WORKSPACE_DIR, repo, token):
        if pfx.get('type') != 'dir':
            continue
        prefix = pfx.get('name')
        if not valid_prefix(prefix):
            continue
        for e in github_sink.list_dir(pfx.get('path'), repo, token):
            if e.get('type') != 'file' or not str(e.get('name', '')).endswith('.json'):
                continue
            raw, _sha = github_sink.get_file(e['path'], repo, token)
            if not raw:
                continue
            try:
                updated = json.loads(raw.decode('utf-8')).get('updated', '')
                ts = datetime.strptime(updated, '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc)
            except (ValueError, AttributeError):
                continue
            if (datetime.now(timezone.utc) - ts).total_seconds() / 86400.0 <= days:
                continue
            tok = e['name'][:-5]  # strip ".json"
            if not valid_token(tok):
                continue
            github_sink.delete_file(_ws_zip(prefix, tok), 'workspace expired: %s' % prefix, repo, token)
            github_sink.delete_file(_ws_meta(prefix, tok), 'workspace expired meta: %s' % prefix, repo, token)
            removed += 1
    return removed


# ---------------------------------------------------------------------------
# Permanent stores: released datasets + usage registry
# ---------------------------------------------------------------------------

def publish_dataset(prefix, zip_bytes, meta_dict):
    """Commit a released bundle + its meta permanently. Returns the bundle's
    html_url, or None on failure/not-configured."""
    if not configured():
        return None
    repo, token = _repo(), _token()
    stamp = _stamp()
    base = '%s/%s/%s' % (DATASETS_DIR, prefix, stamp)
    url = github_sink.put_file(base + '.zip', zip_bytes,
                               'dataset %s %s' % (prefix, stamp), repo, token)
    github_sink.put_file(base + '.json',
                         json.dumps(meta_dict, ensure_ascii=False, indent=2).encode('utf-8'),
                         'dataset meta %s %s' % (prefix, stamp), repo, token)
    return url


def record_usage(entry_dict):
    """Append one permanent usage-registry record under usage-log/<prefix>/, so it
    sits alongside datasets/<prefix>/ for the same run. Returns True on success."""
    if not configured():
        return False
    repo, token = _repo(), _token()
    now = datetime.now(timezone.utc)
    entry = dict(entry_dict)
    entry.setdefault('timestamp', _now_iso())
    prefix = entry.get('prefix', '')
    folder = prefix if valid_prefix(prefix) else '_unknown'
    stamp = now.strftime('%Y%m%d-%H%M%S-%f')
    path = '%s/%s/%s.json' % (USAGE_DIR, folder, stamp)
    url = github_sink.put_file(
        path, json.dumps(entry, ensure_ascii=False, indent=2).encode('utf-8'),
        'usage: %s' % prefix, repo, token)
    return bool(url)


# ---------------------------------------------------------------------------
# Attested-variant registry: a SINGLE growing CSV across every table ever
# processed, recording each uploaded place name that a user adopts onto an
# Indias place. It is a cumulative authority file (variant -> gz_id), not keyed
# by <prefix>, so it lives at the repo root rather than under a run folder.
# ---------------------------------------------------------------------------

ATTESTED_VARIANTS_PATH = 'attested-variants/variants.csv'
_AV_FIELDS = ['variant_name', 'gz_id', 'source_table', 'adopted_at']


def _av_key(variant_name, gz_id, source_table):
    """Dedup identity for one attestation: the same toponym adopted onto the same
    Indias place from the same source table counts once (case-insensitive)."""
    return (str(variant_name).strip().casefold(),
            str(gz_id).strip(), str(source_table).strip())


def record_attested_variants(source_table, attestations):
    """Append (variant_name, gz_id) attestations from one source table to the
    growing ``attested-variants/variants.csv`` registry in the ARCA-data repo.
    Columns (``;``-separated): variant_name; gz_id; source_table; adopted_at (UTC).

    Deduplicates on (variant_name, gz_id, source_table), so re-processing a table
    or many input rows sharing a toponym add at most one attestation each. Fails
    soft: returns the number of NEW rows committed, or 0 on any error / when the
    repo isn't configured — the caller's adoption is never affected.
    """
    repo, token = _repo(), _token()
    if not (repo and token):
        return 0
    incoming = []
    for variant_name, gz_id in attestations:
        v = ('' if variant_name is None else str(variant_name)).strip()
        g = ('' if gz_id is None else str(gz_id)).strip()
        if g.endswith('.0'):            # Int64/float artefact from CSV reads
            g = g[:-2]
        if v and g:
            incoming.append((v, g))
    if not incoming:
        return 0
    try:
        raw, sha = github_sink.get_file(ATTESTED_VARIANTS_PATH, repo, token)
        rows, existing = [], set()
        if raw:
            reader = csv.DictReader(io.StringIO(raw.decode('utf-8', 'replace')),
                                    delimiter=';')
            for r in reader:
                rows.append([r.get('variant_name', ''), r.get('gz_id', ''),
                             r.get('source_table', ''), r.get('adopted_at', '')])
                existing.add(_av_key(r.get('variant_name', ''), r.get('gz_id', ''),
                                     r.get('source_table', '')))
        stamp = _now_iso()
        added = 0
        for v, g in incoming:
            key = _av_key(v, g, source_table)
            if key in existing:
                continue
            existing.add(key)
            rows.append([v, g, source_table, stamp])
            added += 1
        if not added:
            return 0  # nothing new -> no empty commit
        buf = io.StringIO()
        writer = csv.writer(buf, delimiter=';', lineterminator='\n')
        writer.writerow(_AV_FIELDS)
        writer.writerows(rows)
        ok = github_sink.put_file(
            ATTESTED_VARIANTS_PATH, buf.getvalue().encode('utf-8'),
            'attested-variants: +%d from %s' % (added, source_table),
            repo, token, sha=sha)
        return added if ok else 0
    except (OSError, ValueError, UnicodeError, csv.Error):
        return 0


# ---------------------------------------------------------------------------
# Zip helpers (pack/unpack a set of files) — used by the matching stash layer
# ---------------------------------------------------------------------------

def pack_files(paths_and_arcnames):
    """Zip (abs_path, arcname) pairs that exist. Returns zip bytes."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as z:
        import os
        for abs_path, arcname in paths_and_arcnames:
            if os.path.exists(abs_path):
                z.write(abs_path, arcname=arcname)
    buf.seek(0)
    return buf.read()


def unpack_to(zip_bytes, dest_dir):
    """Extract a stash zip (flat arcnames) into dest_dir. Returns the names written."""
    import os
    os.makedirs(dest_dir, exist_ok=True)
    written = []
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as z:
        for name in z.namelist():
            # flat arcnames only (defensive against traversal)
            safe = os.path.basename(name)
            if not safe:
                continue
            with z.open(name) as src, open(os.path.join(dest_dir, safe), 'wb') as out:
                out.write(src.read())
            written.append(safe)
    return written
