"""User-contributed suggestions for prosopography and place/territory records.

There is no persistent local storage, so each submission is sent off-site to a
PRIVATE GitHub repo: prosopography and place/territory suggestions become Issues
(one Markdown table per submission, one label per kind); the bulk officials-list
upload is committed as a CSV file the Issue links to. See github_sink.py.

Spam control (all stateless, so it survives a stateless deploy):
  * Cloudflare Turnstile (primary) when configured, else a signed arithmetic
    challenge as fallback; both ride a signed token that also provides CSRF and
    a timing trap. Plus a hidden honeypot, a per-IP / per-subnet embargo, and a
    global per-hour send cap as a flood backstop.

All submitted free text is length-capped and screened for code/markup so it
stays inert in the issue body and in the committed CSV.
"""
import io
import os
import re
import csv
import json
import time
import random
import threading
import urllib.parse
import urllib.request
import urllib.error
from datetime import datetime

from flask import (request, render_template, redirect, url_for, abort,
                   current_app, send_file)
from . import bp, github_sink, antibot
from .routes import oficiales_csv, gz_entidades_csv, entidades_csv

# --- Field order per kind (owner-defined). Drives the issue-table rows. ---
SCHEMAS = {
    'link': ['OficialID', 'url', 'comment', 'IPAddress', 'User name', 'User email'],
    'office': ['FullOfficialID', 'Titulo', 'Entidad_nombre_generico',
               'Entidad_tipo_generico', 'Emanador', 'Nombramiento_y', 'Posesion_y',
               'Fin_y', 'Primera_Fecha_y', 'Ultima_Fecha_y', 'Fuente', 'comment',
               'IPAddress', 'User name', 'User email'],
    'bio': ['NOM_entero', 'Nacimiento_y', 'Fallecimiento_y', 'N_date_t', 'F_date_t',
            'Nombres', 'particulo', 'Apellidos', 'titulo_de_nobleza', 'religious_order',
            'PlaceOfBirth_Settlement', 'PlaceOfBirth_GZ_ID', 'PlaceOfBirth_Province',
            'PlaceOfBirth_Country', 'Nacimiento_num', 'PlaceOfDeath_Settlement', 'Fuente',
            'Nombres_alternative', 'Apellidos_alternative', 'FullOficialID', 'IPAddress',
            'User name', 'User email', 'submittedTime', 'Comment'],
}

# --- Editable form fields per kind (mapped into the schema on submit) -----
LINK_DOMAIN = ['url']
OFFICE_DOMAIN = ['Titulo', 'Entidad_nombre_generico', 'Entidad_tipo_generico',
                 'Emanador', 'Nombramiento_y', 'Posesion_y', 'Fin_y',
                 'Primera_Fecha_y', 'Ultima_Fecha_y', 'Fuente']
BIO_DOMAIN = ['NOM_entero', 'Nacimiento_y', 'Fallecimiento_y', 'N_date_t', 'F_date_t',
              'Nombres', 'particulo', 'Apellidos', 'titulo_de_nobleza', 'religious_order',
              'PlaceOfBirth_Settlement', 'PlaceOfBirth_Province',
              'PlaceOfBirth_Country', 'PlaceOfDeath_Settlement', 'Fuente',
              'Nombres_alternative', 'Apellidos_alternative']
KIND_FIELDS = {'link': LINK_DOMAIN, 'office': OFFICE_DOMAIN, 'bio': BIO_DOMAIN}
REQUIRED_EXTRA = {'link': ['url'], 'office': ['Fuente'], 'bio': []}

# --- Field metadata for the forms (name, label_es, label_en, hint_es, hint_en) ---
OFFICE_FIELD_META = [
    ('Titulo', 'Título', 'Title', 'p. ej. Gobernador, Obispo, Virrey', 'e.g. Governor, Bishop, Viceroy'),
    ('Entidad_nombre_generico', 'Territorio', 'Territory', 'Nombre del territorio o entidad del cargo', 'Name of the territory/entity of the office'),
    ('Entidad_tipo_generico', 'Tipo de territorio', 'Territory type', 'p. ej. provincia, obispado, audiencia', 'e.g. province, bishopric, audiencia'),
    ('Emanador', 'Emanador', 'Granting authority', 'Autoridad que otorgó el cargo (Corona, virrey…)', 'Authority that granted the office (Crown, viceroy…)'),
    ('Nombramiento_y', 'Año de nombramiento', 'Year of appointment', 'Solo el año', 'Year only'),
    ('Posesion_y', 'Año de toma de posesión', 'Year took office', 'Solo el año', 'Year only'),
    ('Fin_y', 'Año de cese', 'End year', 'Solo el año', 'Year only'),
    ('Primera_Fecha_y', 'Primera fecha atestiguada', 'First attested year', 'Primer año en que consta en el cargo', 'First year attested in the office'),
    ('Ultima_Fecha_y', 'Última fecha atestiguada', 'Last attested year', 'Último año en que consta en el cargo', 'Last year attested in the office'),
    ('Fuente', 'Fuente', 'Source', 'Referencia bibliográfica o archivística (obligatorio)', 'Bibliographic or archival reference (required)'),
]
BIO_FIELD_META = [
    ('NOM_entero', 'Nombre completo', 'Full name', '', ''),
    ('Nacimiento_y', 'Año de nacimiento', 'Birth year', 'Solo el año (número)', 'Year only (number)'),
    ('Fallecimiento_y', 'Año de fallecimiento', 'Death year', 'Solo el año (número)', 'Year only (number)'),
    ('N_date_t', 'Fecha de nacimiento', 'Birth date', 'Fecha o descripción textual', 'Date or textual description'),
    ('F_date_t', 'Fecha de fallecimiento', 'Death date', 'Fecha o descripción textual', 'Date or textual description'),
    ('Nombres', 'Nombre(s) de pila', 'Given name(s)', '', ''),
    ('particulo', 'Partícula', 'Particle', 'p. ej. «de», «de la»', 'e.g. “de”, “de la”'),
    ('Apellidos', 'Apellido(s)', 'Surname(s)', '', ''),
    ('titulo_de_nobleza', 'Títulos de nobleza', 'Titles of nobility', '', ''),
    ('religious_order', 'Orden religiosa', 'Religious order', '', ''),
    ('PlaceOfBirth_Settlement', 'Lugar de nacimiento', 'Place of birth', '', ''),
    ('PlaceOfBirth_Province', 'Provincia de nacimiento', 'Province of birth', '', ''),
    ('PlaceOfBirth_Country', 'País de nacimiento', 'Country of birth', '', ''),
    ('PlaceOfDeath_Settlement', 'Lugar de defunción', 'Place of death', '', ''),
    ('Fuente', 'Fuente', 'Source', 'Referencia que respalda la corrección', 'Reference backing the correction'),
    ('Nombres_alternative', 'Nombres alternativos', 'Alternative given names', '', ''),
    ('Apellidos_alternative', 'Apellidos alternativos', 'Alternative surnames', '', ''),
]

# ============================================================================
#  Place / territory suggestions  ->  one GitHub issue per submission
# ----------------------------------------------------------------------------
# Each place- or territory-related suggestion becomes an issue. Each row carries
# only the columns relevant to the chosen box (the rest stay blank, and empty
# fields are dropped from the issue table); `gz_id` holds the place gz_id OR the
# territory
# Entidad id. Source (Fuente) is required on every box.
LT_SCHEMA = ['gz_id', 'name_attest', 'iglesia_cat', 'category', 'capital',
             'foreignurl', 'claim', 'Desde', 'Hasta', 'Fuente',
             'IPAddress', 'User name', 'User email', 'Comment']

# kind -> editable columns shown in that box's form.
LT_KIND_FIELDS = {
    'pl_name':     ['name_attest', 'Desde', 'Hasta', 'Fuente'],
    'pl_church':   ['iglesia_cat', 'Desde', 'Hasta', 'Fuente'],
    'pl_category': ['category', 'Desde', 'Hasta', 'Fuente'],
    'pl_url':      ['foreignurl'],
    'pl_claim':    ['claim'],
    'pl_source':   ['Fuente', 'foreignurl'],
    'tr_name':     ['name_attest', 'Desde', 'Hasta', 'Fuente'],
    'tr_capital':  ['capital', 'Desde', 'Hasta', 'Fuente'],
    'tr_url':      ['foreignurl'],
    'tr_claim':    ['claim'],
    'tr_source':   ['Fuente', 'foreignurl'],
}
# Required editable fields per box. Fuente is mandatory for data corrections and
# for the source box; the URL and article-claim boxes are self-referential (the
# link / proposal itself is the content) so they only require their own field.
LT_REQUIRED = {
    'pl_name': ['name_attest', 'Fuente'], 'pl_church': ['iglesia_cat', 'Fuente'],
    'pl_category': ['category', 'Fuente'], 'pl_url': ['foreignurl'],
    'pl_claim': ['claim'], 'pl_source': ['Fuente'],
    'tr_name': ['name_attest', 'Fuente'], 'tr_capital': ['capital', 'Fuente'],
    'tr_url': ['foreignurl'], 'tr_claim': ['claim'], 'tr_source': ['Fuente'],
}
LT_PLACE_KINDS = {'pl_name', 'pl_church', 'pl_category', 'pl_url', 'pl_claim', 'pl_source'}
LT_TERR_KINDS = {'tr_name', 'tr_capital', 'tr_url', 'tr_claim', 'tr_source'}
# kind -> title/desc translation-key suffix (shared where the wording matches).
LT_LABEL = {'pl_name': 'name', 'tr_name': 'name', 'pl_church': 'church',
            'pl_category': 'category', 'tr_capital': 'capital',
            'pl_url': 'url', 'tr_url': 'url', 'pl_claim': 'claim', 'tr_claim': 'claim',
            'pl_source': 'source', 'tr_source': 'source'}

# Column -> (es_label, en_label, es_hint, en_hint) for the form fields.
LT_FIELD_META = {
    'name_attest': ('Nombre atestiguado', 'Attested name',
                    'Variante o forma documentada del nombre', 'Documented variant/form of the name'),
    'iglesia_cat': ('Categoría eclesiástica', 'Church category',
                    'p. ej. curato, parroquia, misión, sagrario', 'e.g. parish, mission, cathedral see'),
    'category':    ('Categoría', 'Category',
                    'Categoría del lugar (p. ej. ciudad, villa, pueblo)', 'Category of the place (e.g. city, town, village)'),
    'capital':     ('Capital / cabecera', 'Capital / head town',
                    'Lugar que fue capital del territorio', 'Place that served as the capital of the territory'),
    'foreignurl':  ('Enlace externo (URL)', 'External link (URL)',
                    'URL de otro sitio con información', 'URL of another site with information'),
    'claim':       ('Reclamar / proponer artículo', 'Claim / propose an article',
                    '¿Desea redactar un artículo enciclopédico? Descríbalo', 'Would you like to write an encyclopedic article? Describe it'),
    'Desde':       ('Desde (año)', 'From (year)', 'Solo el año', 'Year only'),
    'Hasta':       ('Hasta (año)', 'To (year)', 'Solo el año', 'Year only'),
    'Fuente':      ('Fuente', 'Source',
                    'Referencia que respalda la sugerencia (obligatorio)', 'Reference backing the suggestion (required)'),
}


def _lt_field_meta(kind):
    """List of (col, es_label, en_label, es_hint, en_hint) for the kind's form."""
    return [(c,) + LT_FIELD_META[c] for c in LT_KIND_FIELDS[kind]]


# --- Limits & validators -------------------------------------------------
MAX_LEN = {'submitter_name': 100, 'submitter_email': 120, 'comment': 2000,
           'url': 500, 'foreignurl': 500, 'claim': 1000}
_DEFAULT_MAX = 300
_BAD_TEXT = re.compile(r'[<>`]|javascript:|data:text/html|vbscript:|\bon\w+\s*=', re.I)
_CTRL = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f]')
_EMAIL = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')
_URL = re.compile(r'^https?://[^\s<>"\'`]{3,}$', re.I)


# --- Anti-bot: shared stateless gate (challenge + Turnstile), see antibot.py --
make_challenge = antibot.make_challenge
turnstile_site_key = antibot.turnstile_site_key


def _antibot_pass(form):
    return antibot.antibot_pass(form, _client_ip())


# --- Anti-bot: per-IP / per-subnet embargo (in-memory) -------------------
_ip_lock = threading.Lock()
_ip_last = {}
_EMBARGO_IP = 60
_EMBARGO_NET = 20


def _client_ip():
    return (request.remote_addr or '').strip()


def _subnet(ip):
    return '.'.join(ip.split('.')[:3]) if ip.count('.') == 3 else ip


def _embargoed():
    ip, now = _client_ip(), time.time()
    with _ip_lock:
        if now - _ip_last.get('ip:' + ip, 0) < _EMBARGO_IP:
            return True
        if now - _ip_last.get('net:' + _subnet(ip), 0) < _EMBARGO_NET:
            return True
    return False


def _record_ip():
    ip, now = _client_ip(), time.time()
    with _ip_lock:
        _ip_last['ip:' + ip] = now
        _ip_last['net:' + _subnet(ip)] = now


# --- Cleaning / validation ----------------------------------------------
def _clean(v, name):
    maxlen = MAX_LEN.get(name, _DEFAULT_MAX)
    v = (v or '').replace('\r\n', '\n').replace('\r', '\n')
    v = _CTRL.sub('', v).strip()
    return v[:maxlen]


def _validate(kind, form):
    name = _clean(form.get('submitter_name'), 'submitter_name')
    email = _clean(form.get('submitter_email'), 'submitter_email')
    comment = _clean(form.get('comment'), 'comment')
    extra = {f: _clean(form.get(f), f) for f in KIND_FIELDS[kind]}

    if not name or not _EMAIL.match(email):
        return None
    for req in REQUIRED_EXTRA[kind]:
        if not extra.get(req):
            return None
    if kind == 'link' and not _URL.match(extra.get('url', '')):
        return None
    # code / markup guard on every free-text field except the URL-validated link
    for key, val in [('submitter_name', name), ('comment', comment)] + list(extra.items()):
        if key == 'url':
            continue
        if val and _BAD_TEXT.search(val):
            return None
    return name, email, comment, extra


# --- Storage: post to GitHub as Issues (no local persistence) ------------
# Spreadsheet formula-injection guard, kept for the ONE thing still serialized
# as CSV: the bulk officials-list file committed to the repo. A value a
# spreadsheet would evaluate as a formula (leading = + - @, tab/CR) gets an
# apostrophe so Excel/LibreOffice keep it literal when the owner opens the file.
_FORMULA_LEAD = ('=', '+', '-', '@')


def _csv_safe(v):
    if v and (v[0] in _FORMULA_LEAD or v[0] in '\t\r'):
        return "'" + v
    return v


def _csv_safe_row(row):
    return {k: (_csv_safe(v) if isinstance(v, str) else v) for k, v in row.items()}


def _issue_table(fields):
    """Render non-empty fields as a GitHub-Markdown table (pipes/newlines escaped)."""
    lines = ['| Field | Value |', '| --- | --- |']
    for k, v in fields.items():
        v = ('' if v is None else str(v)).replace('\r\n', '\n')
        if not v.strip():
            continue
        v = v.replace('|', '\\|').replace('\n', '<br>')
        lines.append('| {} | {} |'.format(k, v))
    return '\n'.join(lines)


# --- Global send cap: a flood backstop on top of Turnstile + per-IP embargo.
# In-memory per process: even with several workers each stays well under
# GitHub's secondary rate limits, and no single process can spew issues.
_send_lock = threading.Lock()
_send_times = []
_SEND_WINDOW = 3600      # seconds
_SEND_MAX = 60           # max accepted submissions per window, globally


def _over_send_cap():
    now = time.time()
    with _send_lock:
        _send_times[:] = [t for t in _send_times if t >= now - _SEND_WINDOW]
        return len(_send_times) >= _SEND_MAX


def _record_send():
    with _send_lock:
        _send_times.append(time.time())


def _append(kind, oficial, name, email, comment, extra):
    """Build the row and open a GitHub issue. Returns True on success."""
    ip = _client_ip()
    now = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')
    row = {c: '' for c in SCHEMAS[kind]}
    if kind == 'link':
        row.update({'OficialID': oficial.get('OficialID', ''), 'url': extra.get('url', ''),
                    'comment': comment, 'IPAddress': ip,
                    'User name': name, 'User email': email})
    elif kind == 'office':
        row['FullOfficialID'] = oficial.get('FullOficialID', '')   # hidden field, added here
        for f in OFFICE_DOMAIN:
            row[f] = extra.get(f, '')
        row.update({'comment': comment, 'IPAddress': ip,
                    'User name': name, 'User email': email})
    else:  # bio
        for f in BIO_DOMAIN:
            row[f] = extra.get(f, '')
        row.update({'FullOficialID': oficial.get('FullOficialID', ''),  # hidden field, added here
                    'IPAddress': ip, 'User name': name, 'User email': email,
                    'submittedTime': now, 'Comment': comment})
    person = oficial.get('NOM_entero') or oficial.get('OficialID', '')
    title = '[{}] {} (persIndias{})'.format(kind, person, oficial.get('OficialID', ''))
    return github_sink.create_issue(title, _issue_table(row),
                                    labels=['suggestion', kind]) is not None


# --- Submit route --------------------------------------------------------
@bp.route('/people/persIndias<OficialID>/suggest/<kind>', methods=['GET', 'POST'])
def people_suggest(OficialID, kind):
    if kind not in SCHEMAS:
        abort(404)
    oficial = next((o for o in oficiales_csv if o.get('OficialID') == OficialID), None)
    if oficial is None:
        abort(404)

    # GET -> the standalone form page (opened in a new tab from the person page).
    if request.method == 'GET':
        return render_template('suggest_form.html', kind=kind, people=oficial,
                               challenge=make_challenge(), turnstile_sitekey=turnstile_site_key(),
                               office_field_meta=OFFICE_FIELD_META,
                               bio_field_meta=BIO_FIELD_META,
                               msg=request.args.get('msg', ''))

    def _done(code):   # PRG: reload the form page with a status banner
        return redirect(url_for('enciclopedia.people_suggest',
                                OficialID=OficialID, kind=kind, msg=code))

    if (request.form.get('website') or '').strip():        # honeypot filled
        return _done('bot')
    if not _antibot_pass(request.form):
        return _done('bot')
    if _embargoed():
        return _done('embargo')
    parsed = _validate(kind, request.form)
    if parsed is None:
        return _done('invalid')
    if _over_send_cap():
        return _done('embargo')

    name, email, comment, extra = parsed
    if not _append(kind, oficial, name, email, comment, extra):
        return _done('senderror')
    _record_ip()
    _record_send()
    return _done('thanks')


# ============================================================================
#  Place / territory suggestion routes
# ============================================================================
def _lt_validate(kind, form):
    name = _clean(form.get('submitter_name'), 'submitter_name')
    email = _clean(form.get('submitter_email'), 'submitter_email')
    comment = _clean(form.get('comment'), 'comment')
    extra = {c: _clean(form.get(c), c) for c in LT_KIND_FIELDS[kind]}

    if not name or not _EMAIL.match(email):
        return None
    for req in LT_REQUIRED[kind]:
        if not extra.get(req):
            return None
    if extra.get('foreignurl') and not _URL.match(extra['foreignurl']):
        return None
    # code / markup guard on every free-text field except the URL-validated link
    for key, val in [('submitter_name', name), ('comment', comment)] + list(extra.items()):
        if key == 'foreignurl':
            continue
        if val and _BAD_TEXT.search(val):
            return None
    return name, email, comment, extra


def _lt_append(entity_id, kind, name, email, comment, extra, entity_name='', scope=''):
    """Build the place/territory row and open a GitHub issue. Returns True on success."""
    row = {c: '' for c in LT_SCHEMA}
    row['gz_id'] = entity_id                         # holds place gz_id OR territory Entidad
    for c in LT_KIND_FIELDS[kind]:
        row[c] = extra.get(c, '')
    row.update({'IPAddress': _client_ip(), 'User name': name,
                'User email': email, 'Comment': comment})
    title = '[{}] {} ({})'.format(kind, entity_name or entity_id, entity_id)
    return github_sink.create_issue(title, _issue_table(row),
                                    labels=['suggestion', scope or 'place']) is not None


def _lt_handle(entity, entity_id, entity_name, scope, kind):
    """Shared GET(form)/POST(store) logic for place and territory boxes."""
    if request.method == 'GET':
        suffix = LT_LABEL[kind]
        return render_template('lt_suggest_form.html', kind=kind, scope=scope,
                               entity=entity, entity_id=entity_id, entity_name=entity_name,
                               field_meta=_lt_field_meta(kind), required_fields=LT_REQUIRED[kind],
                               title_key='sug_title_' + suffix, desc_key='sug_desc_' + suffix,
                               challenge=make_challenge(), turnstile_sitekey=turnstile_site_key(), msg=request.args.get('msg', ''))

    endpoint = 'enciclopedia.place_suggest' if scope == 'place' else 'enciclopedia.territory_suggest'
    id_key = 'place_id' if scope == 'place' else 'territory_id'

    def _done(code):   # PRG: reload the form page with a status banner
        return redirect(url_for(endpoint, **{id_key: entity_id, 'kind': kind, 'msg': code}))

    if (request.form.get('website') or '').strip():        # honeypot filled
        return _done('bot')
    if not _antibot_pass(request.form):
        return _done('bot')
    if _embargoed():
        return _done('embargo')
    parsed = _lt_validate(kind, request.form)
    if parsed is None:
        return _done('invalid')
    if _over_send_cap():
        return _done('embargo')

    name, email, comment, extra = parsed
    if not _lt_append(entity_id, kind, name, email, comment, extra, entity_name, scope):
        return _done('senderror')
    _record_ip()
    _record_send()
    return _done('thanks')


@bp.route('/place/<place_id>/suggest/<kind>', methods=['GET', 'POST'])
def place_suggest(place_id, kind):
    if kind not in LT_PLACE_KINDS:
        abort(404)
    ent = next((p for p in gz_entidades_csv if p.get('gz_id') == place_id), None)
    if ent is None:
        abort(404)
    name = ent.get('label') or ent.get('nombre') or place_id
    return _lt_handle(ent, place_id, name, 'place', kind)


@bp.route('/territory/<territory_id>/suggest/<kind>', methods=['GET', 'POST'])
def territory_suggest(territory_id, kind):
    if kind not in LT_TERR_KINDS:
        abort(404)
    ent = next((t for t in entidades_csv if t.get('Entidad') == territory_id), None)
    if ent is None:
        abort(404)
    name = ent.get('Nombre') or territory_id
    return _lt_handle(ent, territory_id, name, 'territory', kind)


# ============================================================================
#  Whole-list upload of officials for a territory
# ----------------------------------------------------------------------------
# Users download a per-territory template (data/guidebooks/…, renamed to
# <EntidadID>_lista_oficiales.csv and prefilled with the territory id/name/type),
# fill it in, and upload it again. Accepted rows are appended to one collection
# file. Uploads must keep the exact file name and the template's columns.
OFLISTA_TEMPLATE = 'template_lista_oficiales_usuarios.csv'   # under data/guidebooks
OFLISTA_COLUMNS = [
    'OficialNombres', 'OficialParticulas', 'OficialApellidos', 'Titulo', 'Titulo 2',
    'EntidadID', 'Entidad_nombre_generico', 'Entidad_tipo_generico', 'Emanador',
    'Nombramiento_y', 'Posesion_y', 'Fin_y', 'Primera_Fecha_y', 'Ultima_Fecha_y',
    'Fuente', 'Comentario', 'Nombramiento_num', 'Posesion_num', 'Fin_num',
    'Primera Fecha_num', 'Ultima_Fecha_num',
]
OFLISTA_SCHEMA = OFLISTA_COLUMNS + ['IPAddress', 'User name', 'User email',
                                    'submittedTime', 'SourceFile', 'Comment']
_OFLISTA_MAX_BYTES = 2_000_000
_OFLISTA_MAX_ROWS = 5000


def _oflista_template_path():
    return os.path.join(current_app.root_path, 'data', 'guidebooks', OFLISTA_TEMPLATE)


def _oflista_columns():
    """Live template header (source of truth for the 'fields add up' check)."""
    with open(_oflista_template_path(), encoding='utf-8-sig', newline='') as f:
        return next(csv.reader(f, delimiter=';'))


def _oflista_desc_markers():
    """First-cell values of the template's guidance rows, to skip them on upload."""
    with open(_oflista_template_path(), encoding='utf-8-sig', newline='') as f:
        rows = list(csv.reader(f, delimiter=';'))
    return {r[0].strip() for r in rows[1:] if r and r[0].strip()}


def _oflista_expected_name(territory_id):
    return '{}_lista_oficiales.csv'.format(territory_id)


def _oflista_download(territory):
    """Template served as <EntidadID>_lista_oficiales.csv, with the territory's
    id/name/type prefilled in a starter row (guidance rows kept)."""
    cols = _oflista_columns()
    with open(_oflista_template_path(), encoding='utf-8-sig', newline='') as f:
        rows = list(csv.reader(f, delimiter=';'))
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=';')
    for r in rows:
        w.writerow(r)
    idx = {c: i for i, c in enumerate(cols)}
    starter = ['' for _ in cols]
    for col, val in (('EntidadID', territory.get('Entidad', '')),
                     ('Entidad_nombre_generico', territory.get('Nombre', '')),
                     ('Entidad_tipo_generico', territory.get('tipo', ''))):
        if col in idx:
            starter[idx[col]] = val
    w.writerow(starter)
    data = io.BytesIO(buf.getvalue().encode('utf-8-sig'))
    return send_file(data, mimetype='text/csv', as_attachment=True,
                     download_name=_oflista_expected_name(territory.get('Entidad', '')))


def _oflista_parse(territory_id, storage):
    """Validate the uploaded file and return (data_rows, None) or (None, code)."""
    if os.path.basename(storage.filename or '') != _oflista_expected_name(territory_id):
        return None, 'badname'
    raw = storage.read()
    if not raw or len(raw) > _OFLISTA_MAX_BYTES:
        return None, 'badfile'
    text = None
    for enc in ('utf-8-sig', 'latin-1'):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        return None, 'badfile'
    rows = list(csv.reader(io.StringIO(text), delimiter=';'))
    if not rows:
        return None, 'badfile'
    cols = _oflista_columns()
    if [h.strip() for h in rows[0]] != [c.strip() for c in cols]:
        return None, 'badcols'

    markers = _oflista_desc_markers()
    ap = cols.index('OficialApellidos') if 'OficialApellidos' in cols else 2
    out = []
    for r in rows[1:]:
        vals = [_clean(v, '_cell') for v in r]
        vals = (vals + [''] * len(cols))[:len(cols)]
        if vals[0].strip() in markers:            # a template guidance row
            continue
        if not (vals[0].strip() or vals[ap].strip()):   # no official name -> empty row
            continue
        for v in vals:                            # code / markup guard
            if v and _BAD_TEXT.search(v):
                return None, 'badfile'
        out.append(dict(zip(cols, vals)))
        if len(out) > _OFLISTA_MAX_ROWS:
            return None, 'badfile'
    if not out:
        return None, 'empty'
    return out, None


def _oflista_store(rows, name, email, comment, fname, territory_id, territory_name):
    """Commit the uploaded officials list as a CSV in the private repo and open an
    issue linking to it (too large for an issue body). Returns True on success."""
    now = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')
    ip, base = _client_ip(), os.path.basename(fname or '')
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=OFLISTA_SCHEMA, delimiter=';',
                       quoting=csv.QUOTE_MINIMAL, extrasaction='ignore')
    w.writeheader()
    for rec in rows:
        row = dict(rec)
        row.update({'IPAddress': ip, 'User name': name, 'User email': email,
                    'submittedTime': now, 'SourceFile': base, 'Comment': comment})
        w.writerow(_csv_safe_row(row))
    content = buf.getvalue().encode('utf-8-sig')

    stamp = now.replace('-', '').replace(':', '').replace(' ', '_')
    safe = re.sub(r'[^A-Za-z0-9._-]+', '_', base or 'lista.csv')
    path = 'uploads/{}/{}_{}'.format(territory_id, stamp, safe)
    url = github_sink.commit_file(
        path, content,
        'officials list: {} ({}), {} rows'.format(territory_name, territory_id, len(rows)))
    if url is None:
        return False

    fields = {'Territory': '{} ({})'.format(territory_name, territory_id),
              'Rows': len(rows), 'Submitter': name, 'Email': email,
              'IP': ip, 'Source file': base, 'Comment': comment}
    body = _issue_table(fields) + '\n\n[Open the committed CSV]({})\n'.format(url)
    return github_sink.create_issue(
        '[officials-list] {} ({}) — {} rows'.format(territory_name, territory_id, len(rows)),
        body, labels=['suggestion', 'officials-list']) is not None


@bp.route('/territory/<territory_id>/oficiales-template')
def territory_oficiales_template(territory_id):
    ent = next((t for t in entidades_csv if t.get('Entidad') == territory_id), None)
    if ent is None:
        abort(404)
    return _oflista_download(ent)


@bp.route('/territory/<territory_id>/oficiales-lista', methods=['GET', 'POST'])
def territory_oficiales_lista(territory_id):
    ent = next((t for t in entidades_csv if t.get('Entidad') == territory_id), None)
    if ent is None:
        abort(404)

    if request.method == 'GET':
        return render_template('oficiales_lista_form.html', territory=ent,
                               entity_id=territory_id, entity_name=ent.get('Nombre') or territory_id,
                               challenge=make_challenge(), turnstile_sitekey=turnstile_site_key(), msg=request.args.get('msg', ''))

    def _done(code):
        return redirect(url_for('enciclopedia.territory_oficiales_lista',
                                territory_id=territory_id, msg=code))

    if (request.form.get('website') or '').strip():
        return _done('bot')
    if not _antibot_pass(request.form):
        return _done('bot')
    if _embargoed():
        return _done('embargo')
    name = _clean(request.form.get('submitter_name'), 'submitter_name')
    email = _clean(request.form.get('submitter_email'), 'submitter_email')
    comment = _clean(request.form.get('comment'), 'comment')
    if not name or not _EMAIL.match(email):
        return _done('invalid')
    storage = request.files.get('listfile')
    if storage is None or not storage.filename:
        return _done('nofile')
    rows, err = _oflista_parse(territory_id, storage)
    if err:
        return _done(err)
    if _over_send_cap():
        return _done('embargo')

    if not _oflista_store(rows, name, email, comment, storage.filename,
                          territory_id, ent.get('Nombre') or territory_id):
        return _done('senderror')
    _record_ip()
    _record_send()
    return _done('thanks')
