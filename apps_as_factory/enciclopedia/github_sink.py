"""Post user suggestions to a private GitHub repo instead of local CSVs.

The deployment has no persistent storage, so suggestions are sent off-site: each
one becomes an Issue in a private repo (owner-reviewed there), and the bulk
officials-list upload is committed as a file the Issue links to.

Config (app.config, from local_secrets.json / env):
  GITHUB_TOKEN - fine-grained PAT scoped to the ONE private repo, with
                 Issues: Read/Write and Contents: Read/Write.
  GITHUB_REPO  - "owner/name".

Everything here fails soft: a network/API error returns None/False, and the
caller shows a "try again later" banner — nothing is stored locally, nothing is
silently lost.
"""
import json
import base64
import urllib.parse
import urllib.request
import urllib.error

from flask import current_app

_API = 'https://api.github.com'
# In-process cache of labels we've already ensured exist (best-effort, resets on
# restart — re-ensuring is a cheap idempotent no-op).
_known_labels = set()


def _token(token=None):
    return token or current_app.config.get('GITHUB_TOKEN', '')


def _repo(repo=None):
    return repo or current_app.config.get('GITHUB_REPO', '')


def configured():
    return bool(_token() and _repo())


def _request(method, path, payload=None, token=None):
    """Raw GitHub API call. Raises urllib.error.HTTPError on non-2xx."""
    data = json.dumps(payload).encode('utf-8') if payload is not None else None
    req = urllib.request.Request(_API + path, data=data, method=method, headers={
        'Authorization': 'Bearer ' + _token(token),
        'Accept': 'application/vnd.github+json',
        'X-GitHub-Api-Version': '2022-11-28',
        'User-Agent': 'arca-suggestions',
        'Content-Type': 'application/json',
    })
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = resp.read().decode('utf-8')
        return resp.status, (json.loads(body) if body else {})


def _ensure_label(name):
    """Create the label if it doesn't exist yet (so issue creation never 422s on
    an unknown label). Best-effort and cached."""
    if not name or name in _known_labels:
        return
    repo = _repo()
    try:
        _request('GET', '/repos/%s/labels/%s' % (repo, urllib.parse.quote(name)))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            try:
                _request('POST', '/repos/%s/labels' % repo, {'name': name, 'color': 'ededed'})
            except (urllib.error.URLError, OSError):
                pass
    except (urllib.error.URLError, OSError):
        pass
    _known_labels.add(name)


def create_issue(title, body, labels=None):
    """Create an issue; return its number on success, None on failure."""
    if not configured():
        return None
    labels = list(labels or [])
    for lb in labels:
        _ensure_label(lb)
    try:
        _status, data = _request('POST', '/repos/%s/issues' % _repo(),
                                 {'title': (title or 'suggestion')[:250],
                                  'body': body or '', 'labels': labels})
        return data.get('number')
    except (urllib.error.URLError, OSError, ValueError):
        return None


def commit_file(path, content_bytes, message):
    """Create a file in the repo (path must not already exist); return its
    html_url on success, None on failure."""
    if not configured():
        return None
    payload = {'message': message,
               'content': base64.b64encode(content_bytes).decode('ascii')}
    try:
        _status, data = _request(
            'PUT', '/repos/%s/contents/%s' % (_repo(), urllib.parse.quote(path)), payload)
        return (data.get('content') or {}).get('html_url')
    except (urllib.error.URLError, OSError, ValueError):
        return None


# ---------------------------------------------------------------------------
# General file CRUD (repo/token explicit) — backs the data repo's workspace
# stash and permanent stores. Each helper fails soft (None/False/[]).
# ---------------------------------------------------------------------------

def _quote_path(path):
    return '/'.join(urllib.parse.quote(seg) for seg in str(path).split('/'))


def get_file(path, repo, token):
    """Fetch a file's raw bytes and blob sha. Returns (bytes, sha) or (None, None)
    when it doesn't exist / on error."""
    if not (repo and token):
        return (None, None)
    try:
        _status, data = _request(
            'GET', '/repos/%s/contents/%s' % (repo, _quote_path(path)), token=token)
        content = data.get('content') or ''
        raw = base64.b64decode(content) if content else b''
        return (raw, data.get('sha'))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return (None, None)
        return (None, None)
    except (urllib.error.URLError, OSError, ValueError):
        return (None, None)


def get_sha(path, repo, token):
    """Just the blob sha (for update/delete), or None if the file is absent."""
    return get_file(path, repo, token)[1]


def put_file(path, content_bytes, message, repo, token, sha=None):
    """Create or update a file. Pass sha to overwrite an existing file. Returns
    its html_url on success, None on failure."""
    if not (repo and token):
        return None
    payload = {'message': message,
               'content': base64.b64encode(content_bytes).decode('ascii')}
    if sha:
        payload['sha'] = sha
    try:
        _status, data = _request(
            'PUT', '/repos/%s/contents/%s' % (repo, _quote_path(path)), payload, token=token)
        return (data.get('content') or {}).get('html_url')
    except (urllib.error.URLError, OSError, ValueError):
        return None


def put_file_upsert(path, content_bytes, message, repo, token):
    """Create-or-update without the caller tracking sha: look up the current sha
    (if any) and PUT with it. Returns html_url or None."""
    return put_file(path, content_bytes, message, repo, token,
                    sha=get_sha(path, repo, token))


def delete_file(path, message, repo, token, sha=None):
    """Delete a file (looks up its sha when not given). Returns True on success,
    False on failure or when the file is already absent."""
    if not (repo and token):
        return False
    if not sha:
        sha = get_sha(path, repo, token)
    if not sha:
        return False
    try:
        _request('DELETE', '/repos/%s/contents/%s' % (repo, _quote_path(path)),
                 {'message': message, 'sha': sha}, token=token)
        return True
    except (urllib.error.URLError, OSError, ValueError):
        return False


def list_dir(path, repo, token):
    """List a directory's entries as [{'name','path','sha','type'}]. Empty list on
    error or when the directory doesn't exist."""
    if not (repo and token):
        return []
    try:
        _status, data = _request(
            'GET', '/repos/%s/contents/%s' % (repo, _quote_path(path)), token=token)
        if isinstance(data, list):
            return [{'name': e.get('name'), 'path': e.get('path'),
                     'sha': e.get('sha'), 'type': e.get('type')} for e in data]
        return []
    except (urllib.error.URLError, OSError, ValueError):
        return []
