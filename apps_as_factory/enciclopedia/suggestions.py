"""User-contributed suggestions for prosopography records (links, offices, bio).

Rows are appended to the site owner's pre-existing collection files under
data/user_suggestions/ (exact headers preserved):
  * link   -> prosopografia_sitios.csv
  * office -> prosopografia_curriculum.csv
  * bio    -> prosopografia_bio.csv

Design decisions (per the site owner):
  * Bot control is self-hosted: a hidden honeypot field, a timing trap, a small
    signed arithmetic challenge (which also serves as the CSRF token) and a
    per-IP / per-subnet embargo. No external captcha, no third-party calls.
  * No email: suggestions accumulate in the CSVs and are pulled from a token-
    gated admin page, which can also truncate the files.

All submitted free text is length-capped and screened for code/markup so the
stored rows stay inert when later read in the admin page or fed to any tool.
"""
import io
import os
import re
import csv
import time
import random
import threading
from datetime import datetime

from flask import (request, render_template, redirect, url_for, abort,
                   current_app, send_file)
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired

from . import bp
from .routes import oficiales_csv, gz_entidades_csv, entidades_csv

# --- Target files & exact CSV headers (owner-defined) --------------------
FILES = {'link': 'prosopografia_sitios.csv',
         'office': 'prosopografia_curriculum.csv',
         'bio': 'prosopografia_bio.csv'}
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
#  Place / territory suggestions  ->  data/user_suggestions/lugares_territorios.csv
# ----------------------------------------------------------------------------
# A single owner-defined collection file receives every place- and territory-
# related suggestion. Each row carries only the columns relevant to the chosen
# box (the rest stay blank); `gz_id` holds the place gz_id OR the territory
# Entidad id. Source (Fuente) is required on every box.
LT_FILE = 'lugares_territorios.csv'
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


# --- Anti-bot: signed arithmetic challenge (doubles as CSRF token) -------
def _serializer():
    return URLSafeTimedSerializer(current_app.config['SECRET_KEY'], salt='arca-suggest')


def make_challenge():
    a, b = random.randint(2, 9), random.randint(2, 9)
    token = _serializer().dumps({'a': a, 'b': b, 't': int(time.time())})
    return {'a': a, 'b': b, 'token': token}


def _check_challenge(token, answer):
    try:
        p = _serializer().loads(token or '', max_age=7200)
    except (BadSignature, SignatureExpired):
        return False
    if time.time() - p.get('t', 0) < 4:      # submitted too fast => bot
        return False
    try:
        return int(answer) == p['a'] + p['b']
    except (ValueError, TypeError):
        return False


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


# --- Storage -------------------------------------------------------------
def _file_path(filename):
    return os.path.join(current_app.root_path, 'data', 'user_suggestions', filename)


def _sugg_path(kind):
    return _file_path(FILES[kind])


def _write_row(kind, row):
    path = _sugg_path(kind)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    new = not os.path.exists(path)
    with open(path, 'a', encoding='utf-8', newline='') as f:
        w = csv.DictWriter(f, fieldnames=SCHEMAS[kind], delimiter=';',
                           quoting=csv.QUOTE_MINIMAL, extrasaction='ignore')
        if new:
            w.writeheader()
        w.writerow(row)


def _append(kind, oficial, name, email, comment, extra):
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
    _write_row(kind, row)


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
                               challenge=make_challenge(),
                               office_field_meta=OFFICE_FIELD_META,
                               bio_field_meta=BIO_FIELD_META,
                               msg=request.args.get('msg', ''))

    def _done(code):   # PRG: reload the form page with a status banner
        return redirect(url_for('enciclopedia.people_suggest',
                                OficialID=OficialID, kind=kind, msg=code))

    if (request.form.get('website') or '').strip():        # honeypot filled
        return _done('bot')
    if not _check_challenge(request.form.get('_ct'), request.form.get('challenge')):
        return _done('bot')
    if _embargoed():
        return _done('embargo')
    parsed = _validate(kind, request.form)
    if parsed is None:
        return _done('invalid')

    name, email, comment, extra = parsed
    _append(kind, oficial, name, email, comment, extra)
    _record_ip()
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


def _lt_write_row(row):
    path = _file_path(LT_FILE)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    new = not os.path.exists(path)
    with open(path, 'a', encoding='utf-8', newline='') as f:
        w = csv.DictWriter(f, fieldnames=LT_SCHEMA, delimiter=';',
                           quoting=csv.QUOTE_MINIMAL, extrasaction='ignore')
        if new:
            w.writeheader()
        w.writerow(row)


def _lt_append(entity_id, kind, name, email, comment, extra):
    row = {c: '' for c in LT_SCHEMA}
    row['gz_id'] = entity_id                         # holds place gz_id OR territory Entidad
    for c in LT_KIND_FIELDS[kind]:
        row[c] = extra.get(c, '')
    row.update({'IPAddress': _client_ip(), 'User name': name,
                'User email': email, 'Comment': comment})
    _lt_write_row(row)


def _lt_handle(entity, entity_id, entity_name, scope, kind):
    """Shared GET(form)/POST(store) logic for place and territory boxes."""
    if request.method == 'GET':
        suffix = LT_LABEL[kind]
        return render_template('lt_suggest_form.html', kind=kind, scope=scope,
                               entity=entity, entity_id=entity_id, entity_name=entity_name,
                               field_meta=_lt_field_meta(kind), required_fields=LT_REQUIRED[kind],
                               title_key='sug_title_' + suffix, desc_key='sug_desc_' + suffix,
                               challenge=make_challenge(), msg=request.args.get('msg', ''))

    endpoint = 'enciclopedia.place_suggest' if scope == 'place' else 'enciclopedia.territory_suggest'
    id_key = 'place_id' if scope == 'place' else 'territory_id'

    def _done(code):   # PRG: reload the form page with a status banner
        return redirect(url_for(endpoint, **{id_key: entity_id, 'kind': kind, 'msg': code}))

    if (request.form.get('website') or '').strip():        # honeypot filled
        return _done('bot')
    if not _check_challenge(request.form.get('_ct'), request.form.get('challenge')):
        return _done('bot')
    if _embargoed():
        return _done('embargo')
    parsed = _lt_validate(kind, request.form)
    if parsed is None:
        return _done('invalid')

    name, email, comment, extra = parsed
    _lt_append(entity_id, kind, name, email, comment, extra)
    _record_ip()
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
OFLISTA_FILE = 'oficiales_listas.csv'                        # under data/user_suggestions
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


def _oflista_store(rows, name, email, comment, fname):
    path = _file_path(OFLISTA_FILE)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    new = not os.path.exists(path)
    now = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')
    ip, base = _client_ip(), os.path.basename(fname or '')
    with open(path, 'a', encoding='utf-8', newline='') as f:
        w = csv.DictWriter(f, fieldnames=OFLISTA_SCHEMA, delimiter=';',
                           quoting=csv.QUOTE_MINIMAL, extrasaction='ignore')
        if new:
            w.writeheader()
        for rec in rows:
            row = dict(rec)
            row.update({'IPAddress': ip, 'User name': name, 'User email': email,
                        'submittedTime': now, 'SourceFile': base, 'Comment': comment})
            w.writerow(row)


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
                               challenge=make_challenge(), msg=request.args.get('msg', ''))

    def _done(code):
        return redirect(url_for('enciclopedia.territory_oficiales_lista',
                                territory_id=territory_id, msg=code))

    if (request.form.get('website') or '').strip():
        return _done('bot')
    if not _check_challenge(request.form.get('_ct'), request.form.get('challenge')):
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

    _oflista_store(rows, name, email, comment, storage.filename)
    _record_ip()
    return _done('thanks')


# --- Admin (token-gated) -------------------------------------------------
# Every collection file the admin page manages: the three prosopography files,
# the shared place/territory file, and the uploaded officials lists.
ADMIN_SCHEMAS = dict(SCHEMAS, lugares=LT_SCHEMA, oficiales_listas=OFLISTA_SCHEMA)
ADMIN_FILES = dict(FILES, lugares=LT_FILE, oficiales_listas=OFLISTA_FILE)


def _admin_ok():
    tok = current_app.config.get('ADMIN_TOKEN', '')
    return bool(tok) and request.values.get('token') == tok


@bp.route('/admin/suggestions')
def admin_suggestions():
    if not _admin_ok():
        abort(403)
    data = {}
    for kind in ADMIN_SCHEMAS:
        path = _file_path(ADMIN_FILES[kind])
        rows = []
        if os.path.exists(path):
            with open(path, 'r', encoding='utf-8') as f:
                rows = list(csv.DictReader(f, delimiter=';'))
        data[kind] = {'rows': rows, 'columns': ADMIN_SCHEMAS[kind], 'file': ADMIN_FILES[kind]}
    return render_template('admin_suggestions.html', data=data,
                           token=request.values.get('token'))


@bp.route('/admin/suggestions/download/<kind>')
def admin_suggestions_download(kind):
    if not _admin_ok():
        abort(403)
    if kind not in ADMIN_SCHEMAS:
        abort(404)
    path = _file_path(ADMIN_FILES[kind])
    if not os.path.exists(path):
        abort(404)
    return send_file(path, as_attachment=True, download_name=ADMIN_FILES[kind])


@bp.route('/admin/suggestions/clear', methods=['POST'])
def admin_suggestions_clear():
    if not _admin_ok():
        abort(403)
    kind = request.values.get('kind', '')
    kinds = list(ADMIN_SCHEMAS) if kind == 'all' else [kind]
    for k in kinds:
        if k not in ADMIN_SCHEMAS:
            continue
        path = _file_path(ADMIN_FILES[k])
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w', encoding='utf-8', newline='') as f:
            csv.writer(f, delimiter=';', quoting=csv.QUOTE_MINIMAL).writerow(ADMIN_SCHEMAS[k])
    return redirect(url_for('enciclopedia.admin_suggestions',
                            token=request.values.get('token')))
