"""Shared, stateless anti-bot gate for public POST forms (suggestions + the
matching download gate).

A signed timed token (`_ct`) always provides CSRF (its signature proves we served
the form) and a timing trap (its timestamp). On top of that: Cloudflare Turnstile
when configured, else a signed arithmetic challenge as a dev fallback. All of it
survives a stateless deploy — no server-side session needed.
"""
import time
import json
import random
import urllib.parse
import urllib.request
import urllib.error

from flask import current_app
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired

_SALT = 'arca-suggest'
_TURNSTILE_VERIFY_URL = 'https://challenges.cloudflare.com/turnstile/v0/siteverify'
_MIN_FILL_SECONDS = 4     # a human takes at least this long to fill a form


def _serializer():
    return URLSafeTimedSerializer(current_app.config['SECRET_KEY'], salt=_SALT)


def make_challenge():
    a, b = random.randint(2, 9), random.randint(2, 9)
    token = _serializer().dumps({'a': a, 'b': b, 't': int(time.time())})
    return {'a': a, 'b': b, 'token': token}


def turnstile_site_key():
    """Public site key for the widget (empty -> Turnstile not configured)."""
    return current_app.config.get('TURNSTILE_SITE_KEY', '')


def turnstile_configured():
    return bool(current_app.config.get('TURNSTILE_SECRET_KEY', ''))


def verify_turnstile(token, remote_ip=''):
    """Server-side check of the widget token against Cloudflare. Fail-closed."""
    if not token:
        return False
    data = urllib.parse.urlencode({
        'secret': current_app.config.get('TURNSTILE_SECRET_KEY', ''),
        'response': token,
        'remoteip': remote_ip or '',
    }).encode('utf-8')
    try:
        req = urllib.request.Request(_TURNSTILE_VERIFY_URL, data=data)
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read().decode('utf-8'))
        return bool(result.get('success'))
    except (urllib.error.URLError, OSError, ValueError):
        return False


def antibot_pass(form, remote_ip=''):
    """One gate for every form: valid signed token, not submitted too fast, and
    Turnstile (when configured) or the arithmetic answer as a fallback."""
    if (form.get('website') or '').strip():   # honeypot filled => bot
        return False
    try:
        p = _serializer().loads(form.get('_ct') or '', max_age=7200)
    except (BadSignature, SignatureExpired):
        return False
    if time.time() - p.get('t', 0) < _MIN_FILL_SECONDS:
        return False
    if turnstile_configured():
        return verify_turnstile(form.get('cf-turnstile-response'), remote_ip)
    try:
        return int(form.get('challenge')) == p['a'] + p['b']
    except (ValueError, TypeError):
        return False
