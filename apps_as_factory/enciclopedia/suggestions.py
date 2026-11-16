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
from .routes import oficiales_csv, gz_entidades_csv, entidades_csv, fuentes_csv
from i18n import resolve_locale

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
LT_SCHEMA = ['gz_id', 'name_attest', 'iglesia_cat', 'category', 'categoria_esp', 'capital',
             'foreignurl', 'claim', 'Desde', 'Hasta', 'Fuente',
             'IPAddress', 'User name', 'User email', 'Comment']

# kind -> editable columns shown in that box's form.
LT_KIND_FIELDS = {
    'pl_name':     ['name_attest', 'Desde', 'Hasta', 'Fuente'],
    'pl_church':   ['iglesia_cat', 'Desde', 'Hasta', 'Fuente'],
    'pl_category': ['category', 'categoria_esp', 'Desde', 'Hasta', 'Fuente'],
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
                    'Categoría general del lugar', 'General category of the place'),
    'categoria_esp': ('Categoría específica', 'Specific category',
                      'Tal como consta en la fuente, p. ej. pueblo de indios, real de minas, hacienda',
                      'As given in the source, e.g. pueblo de indios, real de minas, hacienda'),
    'capital':     ('Capital / cabecera', 'Capital / head town',
                    'Lugar que fue capital del territorio', 'Place that served as the capital of the territory'),
    'foreignurl':  ('Enlace externo (URL)', 'External link (URL)',
                    'URL de otro sitio con información', 'URL of another site with information'),
    'claim':       ('Reclamar / proponer artículo', 'Claim / propose an article',
                    '¿Desea redactar un artículo enciclopédico? Descríbalo', 'Would you like to write an encyclopedic article? Describe it'),
    'Desde':       ('Desde (año)', 'From (year)', 'Solo el año', 'Year only'),
    'Hasta':       ('Hasta (año)', 'To (year)', 'Solo el año', 'Year only'),
    'Fuente':      ('Fuente', 'Source',
                    'Cita completa de la referencia que respalda la sugerencia (autor, título, año, páginas o signatura)',
                    'Full citation of the reference backing the suggestion (author, title, year, pages or shelfmark)'),
}

# General place categories for the category box (drop-down), as used in
# gz_categoria.csv `categoria`: (stored value, es_label, en_label).
PL_CATEGORIES = [
    ('Ciudad', 'Ciudad', 'City'),
    ('Villa', 'Villa', 'Town (villa)'),
    ('Pueblo', 'Pueblo', 'Village (pueblo)'),
    ('Poblacion', 'Población', 'Settlement'),
    ('Rural', 'Rural (hacienda, rancho, estancia…)', 'Rural (hacienda, rancho, estancia…)'),
    ('Localidad', 'Localidad', 'Locality'),
    ('Fuerte', 'Fuerte', 'Fort'),
    ('Parcialidad', 'Parcialidad', 'Parcialidad (ward or subject community)'),
]
_PL_CATEGORY_VALUES = {c[0] for c in PL_CATEGORIES}


# --- Source boxes (pl_source / tr_source): RIS-structured source records -----
# The user picks primary vs secondary, then one of 14 document types; the form
# then asks for the RIS fields that type needs (R = required, O = optional),
# per the matrix in data/guidebooks/ris_source_fields_by_type.csv (editable;
# the source of truth). Each submission is appended as one row to a single CSV
# table in the private suggestions repo, with a fresh numeric `fuente` ID, and
# an Issue linking to it is opened for review (the email lives only there).
# class -> (es_label, en_label)
SOURCE_CLASSES = {
    'primary':   ('Fuente primaria', 'Primary source'),
    'secondary': ('Fuente secundaria', 'Secondary source'),
}
# (key, class, es_label, en_label, RIS TY). The TY codes and the stored doc_type
# label are fallbacks: the live values come from the owner's Types sheet,
# data/guidebooks/ris_source_types.csv (matched on its English label), see
# _src_type_sheet().
SOURCE_TYPES = [   # in dropdown order within each class
    ('arch_doc',     'primary',   'Documento de archivo', 'Archival document', 'MANSCPT'),
    ('crit_edition', 'primary',   'Edición crítica', 'Critical edition', 'BOOK'),
    ('hist_map',     'primary',   'Mapa histórico (antes de 1824)', 'Historical map (before 1824)', 'MAP'),
    ('print',        'primary',   'Impreso', 'Print', 'BOOK'),
    ('arch_series',  'primary',   'Serie / fondo de archivo', 'Archival series / holding', 'GEN'),
    ('monograph',    'secondary', 'Monografía', 'Monograph', 'BOOK'),
    ('edited_vol',   'secondary', 'Obra colectiva (volumen editado)', 'Edited volume', 'EDBOOK'),
    ('chapter',      'secondary', 'Capítulo', 'Chapter', 'CHAP'),
    ('article',      'secondary', 'Artículo', 'Article', 'JOUR'),
    ('online_schol', 'secondary', 'Fuente académica en línea', 'Scholarly online source', 'ELEC'),
    ('thesis',       'secondary', 'Tesis', 'Thesis', 'THES'),
    ('arch_catalog', 'secondary', 'Catálogo de archivo / instrumento de descripción', 'Archival catalogue / finding aid', 'CTLG'),
    ('database',     'secondary', 'Base de datos', 'Database', 'DBASE'),
    ('modern_map',   'secondary', 'Mapa moderno (después de 1824)', 'Modern map (after 1824)', 'MAP'),
    ('online_other', 'secondary', 'Otra fuente en línea', 'Other online source', 'ELEC'),
]
# Shorter labels for the type dropdown (es, en). The labels above stay the
# canonical names: they match the Types sheet and the doc_type column.
SOURCE_TYPE_MENU = {
    'arch_doc': ('Documento de archivo', 'Archival document'),
    'crit_edition': ('Edición (crítica)', '(Critical) Edition'),
    'hist_map': ('Mapa histórico', 'Historical map'),
    'print': ('Impreso', 'Print'),
    'arch_series': ('Serie de archivo', 'Archival series'),
    'monograph': ('Monografía', 'Monograph'),
    'edited_vol': ('Obra colectiva', 'Edited volume'),
    'chapter': ('Capítulo', 'Chapter'),
    'article': ('Artículo', 'Article'),
    'online_schol': ('Fuente en línea (académica)', 'Online source (scholarly)'),
    'thesis': ('Tesis', 'Thesis'),
    'arch_catalog': ('Catálogo / instrumento de descripción', 'Catalogue / Finding aid'),
    'database': ('Base de datos', 'Database'),
    'modern_map': ('Mapa moderno', 'Modern map'),
    'online_other': ('Fuente en línea (no académica)', 'Online source (non-academic)'),
}
_SOURCE_TYPE_BY_KEY = {s[0]: s for s in SOURCE_TYPES}
LT_SOURCE_KINDS = {'pl_source', 'tr_source'}

SRC_MATRIX_FILE = 'ris_source_fields_by_type.csv'      # under data/guidebooks
SRC_TYPES_FILE = 'ris_source_types.csv'                 # under data/guidebooks
# Matrix column header -> document-type key.
_SRC_MATRIX_COLS = {
    'Arch. series': 'arch_series', 'Arch. document': 'arch_doc', 'Hist. map': 'hist_map',
    'Print': 'print', 'Critical edition': 'crit_edition', 'Modern map': 'modern_map',
    'Finding aid': 'arch_catalog', 'Monograph': 'monograph', 'Edited volume': 'edited_vol',
    'Chapter': 'chapter',
    'Article': 'article', 'Thesis': 'thesis', 'Scholarly online': 'online_schol',
    'Database': 'database', 'Other online': 'online_other',
}
# Filled by the app, never asked in the form.
SRC_AUTO_FIELDS = ('fuenteID', 'class', 'doc_type', 'ris_type')
# Stored table: the RIS template columns + provenance / relation columns.
SRC_TABLE_PATH = 'sources/fuentes_sugeridas.csv'
SRC_TABLE_COLUMNS = [
    'fuenteID', 'class', 'doc_type', 'ris_type', 'author', 'title', 'year', 'date',
    'date_text', 'editor', 'translator', 'short_title', 'container_title',
    'series_title', 'volume', 'num_volumes', 'issue', 'edition', 'place', 'publisher',
    'page_start', 'page_end', 'scale', 'archive', 'shelfmark', 'original_pub',
    'language', 'isbn_issn', 'doi', 'url', 'accessed', 'keywords', 'notes',
    'coverage', 'entity_id', 'added_by', 'submitted',
    # written by the owner's review tool (review_suggestions/); listed here so a
    # later submission's rewrite of the table keeps them
    'review_status', 'reviewed_at', 'review_note',
]
# Is the source about the place/territory as a whole, or does it only cover it?
SRC_COVERAGE = ('dedicated', 'covers')
# field -> (es_label, en_label, es_hint, en_hint, input kind)
#   input kinds: text | lines (one entry per line, stored '; '-joined) |
#                textarea | year (4 digits) | date (stored YYYY/MM/DD) | url
SRC_FIELD_META = {
    'author': ('Autor(es)', 'Author(s)',
               'Autor, cartógrafo o creador; uno por línea, como «Apellido, Nombre»',
               'Author, cartographer or creator; one per line, as "Surname, Forename"', 'lines'),
    'title': ('Título', 'Title',
              'Título, o una descripción si el material de archivo no tiene título',
              'Title, or a description for untitled archival material', 'text'),
    'year': ('Año', 'Year', 'Año de creación o publicación (cuatro cifras)',
             'Year of creation or publication (four digits)', 'year'),
    'date': ('Fecha exacta', 'Exact date', '', '', 'date'),
    'date_text': ('Fecha (tal como consta)', 'Date (as given)',
                  'Para rangos y fechas inciertas (ca. 1750, 1701–1714)',
                  'For ranges and uncertain dates (ca. 1750, 1701–1714)', 'text'),
    'editor': ('Editor(es)', 'Editor(s)', 'Editor u otro colaborador; uno por línea',
               'Editor or secondary contributor; one per line', 'lines'),
    'translator': ('Traductor(es)', 'Translator(s)', 'Uno por línea', 'One per line', 'lines'),
    'short_title': ('Título abreviado', 'Short title',
                    'Título corto, sigla o abreviatura del archivo',
                    'Short title, siglum or archive abbreviation', 'text'),
    'container_title': ('Obra o serie contenedora', 'Container title',
                        'Revista, libro, sitio web, atlas, o fondo / serie de archivo',
                        'Journal, book, website, atlas, or archival fonds / series', 'text'),
    'series_title': ('Serie de publicación', 'Publication series', '', '', 'text'),
    'volume': ('Volumen', 'Volume', '', '', 'text'),
    'num_volumes': ('Número de volúmenes', 'Number of volumes',
                    'Total de una obra en varios volúmenes', 'Total of a multi-volume work', 'text'),
    'issue': ('Número (fascículo)', 'Issue', '', '', 'text'),
    'edition': ('Edición', 'Edition', 'Edición, estado del mapa o versión de la base de datos',
                'Edition, map state or database version', 'text'),
    'place': ('Lugar', 'Place', 'Lugar de publicación o de redacción, o ciudad del archivo',
              'Place of publication, place of writing, or archive city', 'text'),
    'publisher': ('Editorial / institución', 'Publisher / institution',
                  'Editorial, imprenta, universidad o institución responsable',
                  'Publisher, printer, university or responsible institution', 'text'),
    'page_start': ('Página inicial', 'First page',
                   'También folios, extensión o dimensiones del mapa',
                   'Also folios, extent or map dimensions', 'text'),
    'page_end': ('Página final', 'Last page', 'Última página o folio', 'Last page or folio', 'text'),
    'scale': ('Escala', 'Scale', 'Escala del mapa (extensión, en series de archivo)',
              'Map scale (extent, for archival series)', 'text'),
    'archive': ('Archivo / biblioteca', 'Archive / library', 'Archivo o biblioteca que lo custodia',
                'Holding archive or library', 'text'),
    'shelfmark': ('Signatura', 'Shelfmark', 'Código de referencia o signatura en el archivo',
                  'Reference code or signature within the archive', 'text'),
    'original_pub': ('Publicación original', 'Original publication',
                     'Fecha o fuente original del texto editado',
                     'Original date or source of the edited text', 'text'),
    'language': ('Idioma', 'Language', '', '', 'text'),
    'isbn_issn': ('ISBN / ISSN', 'ISBN / ISSN', '', '', 'text'),
    'doi': ('DOI', 'DOI', 'p. ej. 10.1234/abcd', 'e.g. 10.1234/abcd', 'text'),
    'url': ('Enlace (URL)', 'Link (URL)', 'Dirección web de la fuente o de su copia digital',
            'Web address of the source or its digital copy', 'url'),
    'accessed': ('Fecha de consulta', 'Accessed', '', '', 'date'),
    'keywords': ('Palabras clave', 'Keywords', 'Una por línea', 'One per line', 'lines'),
    'notes': ('Notas', 'Notes', '', '', 'textarea'),
}
_YEAR = re.compile(r'^\d{4}$')
_ISO_DATE = re.compile(r'^(\d{4})[-/](\d{2})[-/](\d{2})$')


def _src_matrix():
    """[(field, required_type_keys, optional_type_keys)] in matrix order, minus
    the auto-filled fields. Read live so edits to the CSV apply without code."""
    path = os.path.join(current_app.root_path, 'data', 'guidebooks', SRC_MATRIX_FILE)
    out = []
    with open(path, encoding='utf-8-sig', newline='') as f:
        for r in csv.DictReader(f):
            fld = (r.get('Field') or '').strip()
            if not fld or fld in SRC_AUTO_FIELDS:
                continue
            req = {k for col, k in _SRC_MATRIX_COLS.items() if (r.get(col) or '').strip().upper() == 'R'}
            opt = {k for col, k in _SRC_MATRIX_COLS.items() if (r.get(col) or '').strip().upper() == 'O'}
            out.append((fld, req, opt))
    return out


def _norm_label(s):
    return re.sub(r'\s*/\s*', '/', ' '.join((s or '').split())).lower()


def _src_type_sheet():
    """{type_key: (doc_type_label, ris_type)} from the owner's Types sheet, read
    live; types missing from the sheet fall back to SOURCE_TYPES."""
    out = {s[0]: (s[3], s[4]) for s in SOURCE_TYPES}
    by_label = {_norm_label(s[3]): s[0] for s in SOURCE_TYPES}
    path = os.path.join(current_app.root_path, 'data', 'guidebooks', SRC_TYPES_FILE)
    try:
        with open(path, encoding='utf-8-sig', newline='') as f:
            for r in csv.DictReader(f):
                key = by_label.get(_norm_label(r.get('doc_type')))
                ty = (r.get('ris_type') or '').strip()
                if key and ty:
                    out[key] = ((r.get('doc_type') or '').strip(), ty)
    except OSError:
        pass
    return out


def _src_type_key(val):
    """Resolve a type given as key or as an EN / ES / Types-sheet label."""
    v = _norm_label(val)
    if not v:
        return None
    if v in _SOURCE_TYPE_BY_KEY:
        return v
    for key, (lbl, _ty) in _src_type_sheet().items():
        if _norm_label(lbl) == v:
            return key
    for s in SOURCE_TYPES:
        if v in (_norm_label(s[2]), _norm_label(s[3])):
            return s[0]
    return None


def _src_form_fields():
    """Template rows: (field, es_label, en_label, es_hint, en_hint, kind, req, opt)
    with req/opt as space-separated type keys (read by the form's JS)."""
    rows = []
    for fld, req, opt in _src_matrix():
        meta = SRC_FIELD_META.get(fld, (fld, fld, '', '', 'text'))
        rows.append((fld,) + meta + (' '.join(sorted(req)), ' '.join(sorted(opt))))
    return rows


def _lt_field_meta(kind):
    """List of (col, es_label, en_label, es_hint, en_hint) for the kind's form."""
    return [(c,) + LT_FIELD_META[c] for c in LT_KIND_FIELDS[kind]]


# --- Limits & validators -------------------------------------------------
MAX_LEN = {'submitter_name': 100, 'submitter_email': 120, 'comment': 2000,
           'url': 500, 'foreignurl': 500, 'claim': 1000, 'Fuente': 600,
           # source-table fields (others use the default)
           'title': 600, 'author': 1000, 'editor': 600, 'translator': 400,
           'container_title': 600, 'keywords': 600, 'notes': 2000}
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
    if 'category' in extra and extra['category'] not in _PL_CATEGORY_VALUES:
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


# ---------------------------------------------------------------------------
#  Source boxes: validate against the type matrix, append to the source table
# ---------------------------------------------------------------------------
def _src_validate(form):
    """Return (name, email, comment, record) or None. `record` holds only the
    fields the chosen document type allows, normalised for storage."""
    name = _clean(form.get('submitter_name'), 'submitter_name')
    email = _clean(form.get('submitter_email'), 'submitter_email')
    comment = _clean(form.get('comment'), 'comment')
    if not name or not _EMAIL.match(email):
        return None
    for val in (name, comment):
        if val and _BAD_TEXT.search(val):
            return None

    rec, _err = _src_record(form.get('source_class'), form.get('source_type'),
                            form.get('coverage'), form.get, name)
    if rec is None:
        return None
    return name, email, comment, rec


def _src_record(s_class, s_type, coverage, get, added_by, bulk=False):
    """Validate one source against the type matrix. `get(field)` returns the raw
    value. Returns (record, None) or (None, (problem, field)); problem is one of
    class | type | coverage | missing | format | notallowed. In bulk mode the
    class may be left empty (derived from the type), multi-value fields may be
    ';'-separated, and a value in a field the type does not use is an error
    instead of being dropped silently."""
    s_class = (s_class or '').strip().lower()
    key = _src_type_key(s_type)
    if key is None:
        return None, ('type', 'doc_type')
    st = _SOURCE_TYPE_BY_KEY[key]
    if bulk and not s_class:
        s_class = st[1]
    if s_class not in SOURCE_CLASSES or st[1] != s_class:
        return None, ('class', 'class')
    coverage = (coverage or '').strip().lower()
    if coverage not in SRC_COVERAGE:
        return None, ('coverage', 'coverage')

    rec = {}
    for fld, req, opt in _src_matrix():
        kind = SRC_FIELD_META.get(fld, ('', '', '', '', 'text'))[4]
        val = _clean(get(fld), fld)
        if key not in req and key not in opt:
            if bulk and val:
                return None, ('notallowed', fld)
            continue                                    # not asked for this type
        if kind == 'lines':
            parts = re.split(r'[\n;]' if bulk else r'\n', val)
            val = '; '.join(x.strip() for x in parts if x.strip())
        elif kind != 'textarea':
            val = ' '.join(val.split())                 # single-line fields
        if not val:
            if key in req:
                return None, ('missing', fld)
            continue
        if kind == 'year' and not _YEAR.match(val):
            return None, ('format', fld)
        if kind == 'date':
            m = _ISO_DATE.match(val)
            if not m:
                return None, ('format', fld)
            val = '/'.join(m.groups())                  # RIS DA / Y2: YYYY/MM/DD
        if kind == 'url':
            if not _URL.match(val):
                return None, ('format', fld)
        elif _BAD_TEXT.search(val):
            return None, ('format', fld)
        rec[fld] = val

    label, ris = _src_type_sheet()[key]
    rec.update({'class': s_class, 'doc_type': label, 'ris_type': ris,
                'coverage': coverage, 'added_by': added_by})
    return rec, None


def _src_base_id():
    """Highest numeric id already used by the published source list, so
    suggested sources never collide with it once merged. The numeric id column
    of fuentes.csv has been renamed over time (fuente / fuenteID / ftid), so
    take the max over every candidate column's numeric values."""
    ids = [int(v) for r in fuentes_csv
           for v in ((r.get(c) or '').strip() for c in ('ftid', 'fuenteID', 'fuente'))
           if v.isdigit()]
    return max(ids) if ids else 0


def _src_store(recs, name, email, comment, entity_id, entity_name, scope, fname=''):
    """Append the records to the source table in the private repo with
    consecutive numeric `fuente` ids, then open one review Issue linking to it.
    Returns the list of new ids on success, None on failure. Retries on a
    concurrent-write conflict."""
    repo, token = github_sink._repo(), github_sink._token()
    if not (repo and token):
        return None
    now = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')
    for _attempt in range(3):
        raw, sha = github_sink.get_file(SRC_TABLE_PATH, repo, token)
        if sha and not raw:
            # Exists but came back without content (Contents API omits files
            # > 1 MB): never rewrite the table from an empty read.
            return None
        rows = []
        if raw:
            rows = list(csv.DictReader(io.StringIO(raw.decode('utf-8-sig')), delimiter=';'))
        for r in rows:                     # tables written before the fuente -> fuenteID rename
            if not r.get('fuenteID') and r.get('fuente'):
                r['fuenteID'] = r['fuente']
        used = [int(r['fuenteID']) for r in rows if (r.get('fuenteID') or '').strip().isdigit()]
        first = max([_src_base_id()] + used) + 1
        ids = list(range(first, first + len(recs)))

        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=SRC_TABLE_COLUMNS, delimiter=';',
                           quoting=csv.QUOTE_MINIMAL, extrasaction='ignore', restval='')
        w.writeheader()
        for r in rows:
            w.writerow(r)
        for new_id, rec in zip(ids, recs):
            row = {c: '' for c in SRC_TABLE_COLUMNS}
            row.update(rec)
            row.update({'fuenteID': str(new_id), 'entity_id': entity_id, 'submitted': now})
            w.writerow(_csv_safe_row(row))
        if len(recs) == 1:
            msg = 'source {}: {} -> {} ({})'.format(ids[0], recs[0].get('title', '')[:60],
                                                    entity_name, entity_id)
        else:
            msg = 'sources {}-{}: {} rows -> {} ({})'.format(ids[0], ids[-1], len(recs),
                                                             entity_name, entity_id)
        url = github_sink.put_file(SRC_TABLE_PATH, buf.getvalue().encode('utf-8-sig'),
                                   msg, repo, token, sha=sha)
        if url:
            break
        time.sleep(0.5)            # likely a stale sha (concurrent write): re-read
    else:
        return None

    related = {'Related {}'.format(scope): '{} ({})'.format(entity_name, entity_id)}
    contact = {'Submitter': name, 'Email': email, 'IP': _client_ip(),
               'Source file': os.path.basename(fname or ''), 'Comment': comment}
    if len(recs) == 1:
        rec = recs[0]
        fields = dict({'fuenteID': ids[0]}, **related)
        fields.update({'Coverage': rec['coverage'],
                       'Type': '{} / {} ({})'.format(rec['class'], rec['doc_type'], rec['ris_type']),
                       'Title': rec.get('title', ''), 'Author': rec.get('author', ''),
                       'Year': rec.get('year', '')})
        fields.update(contact)
        body = _issue_table(fields)
        title = '[source {}] {} ({})'.format(ids[0], entity_name, entity_id)
    else:
        fields = dict({'fuenteID': '{}–{}'.format(ids[0], ids[-1]), 'Rows': len(recs)}, **related)
        fields.update(contact)
        lines = ['| fuenteID | Type | Title | Year |', '| --- | --- | --- | --- |']
        for new_id, rec in list(zip(ids, recs))[:100]:
            cells = [rec['doc_type'], rec.get('title', '')[:120], rec.get('year', '')]
            lines.append('| {} | {} |'.format(new_id, ' | '.join(
                c.replace('|', '\\|').replace('\n', ' ') for c in cells)))
        if len(recs) > 100:
            lines.append('\n… and {} more rows in the table.'.format(len(recs) - 100))
        body = _issue_table(fields) + '\n\n' + '\n'.join(lines)
        title = '[sources {}–{}] {} ({}) — {} rows'.format(ids[0], ids[-1], entity_name,
                                                          entity_id, len(recs))
    body += '\n\n[Open the source table]({})\n'.format(url)
    github_sink.create_issue(title, body, labels=['suggestion', 'source', scope])
    # The rows are already committed; a failed Issue only loses the notification.
    return ids


def _lt_handle(entity, entity_id, entity_name, scope, kind):
    """Shared GET(form)/POST(store) logic for place and territory boxes."""
    is_source = kind in LT_SOURCE_KINDS
    if request.method == 'GET':
        suffix = LT_LABEL[kind]
        return render_template('lt_suggest_form.html', kind=kind, scope=scope,
                               entity=entity, entity_id=entity_id, entity_name=entity_name,
                               field_meta=[] if is_source else _lt_field_meta(kind),
                               place_categories=PL_CATEGORIES,
                               required_fields=LT_REQUIRED[kind],
                               title_key='sug_title_' + suffix, desc_key='sug_desc_' + suffix,
                               source_classes=SOURCE_CLASSES if is_source else None,
                               source_types=SOURCE_TYPES, source_type_menu=SOURCE_TYPE_MENU,
                               source_fields=_src_form_fields() if is_source else [],
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
    parsed = _src_validate(request.form) if is_source else _lt_validate(kind, request.form)
    if parsed is None:
        return _done('invalid')
    if _over_send_cap():
        return _done('embargo')

    name, email, comment, extra = parsed
    if is_source:
        ok = _src_store([extra], name, email, comment, entity_id, entity_name, scope) is not None
    else:
        ok = _lt_append(entity_id, kind, name, email, comment, extra, entity_name, scope)
    if not ok:
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
#  Whole-set upload of sources for a place or territory
# ----------------------------------------------------------------------------
# Users download <id>_fuentes.csv (entity_id prefilled, two guidance rows whose
# first cell starts with '#'), fill one row per source and upload it again.
# Every row is checked with the same rules as the single-source form; the file
# is accepted only if all rows pass, then appended to the source table with
# consecutive ids and announced in one review Issue.
SRC_BULK_LEAD = ['entity_id', 'coverage', 'class', 'doc_type']
_SRC_BULK_IGNORED = {'fuenteID', 'fuente', 'genre', 'ris_type'}      # tolerated if pasted from the table
_SRC_BULK_MAX_BYTES = 2_000_000
_SRC_BULK_MAX_ROWS = 1000


def _src_bulk_columns():
    return SRC_BULK_LEAD + [fld for fld, _r, _o in _src_matrix()]


def _src_entity(scope, entity_id):
    """(entity, name) for a place / territory id, or (None, None)."""
    if scope == 'place':
        ent = next((p for p in gz_entidades_csv if p.get('gz_id') == entity_id), None)
        return ent, (ent and (ent.get('label') or ent.get('nombre') or entity_id))
    ent = next((t for t in entidades_csv if t.get('Entidad') == entity_id), None)
    return ent, (ent and (ent.get('Nombre') or entity_id))


def _src_col_label(col, lang):
    lead = {'entity_id': ('ID del lugar / territorio', 'Place / territory ID'),
            'coverage': ('Relación', 'Relation'),
            'class': ('Tipo de recurso', 'Type of resource'),
            'doc_type': ('Clase de recurso', 'Kind of resource')}
    meta = lead.get(col) or SRC_FIELD_META.get(col, (col, col))
    return meta[1] if lang == 'en' else meta[0]


def _src_csv_response(rows, fname):
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=';')
    for r in rows:
        w.writerow(r)
    return send_file(io.BytesIO(buf.getvalue().encode('utf-8-sig')), mimetype='text/csv',
                     as_attachment=True, download_name=fname)


def _src_template(entity_id):
    lang = resolve_locale()
    cols = _src_bulk_columns()
    en = lang == 'en'
    fmt = {
        'entity_id': 'prefilled — leave as is' if en else 'precargado — no modificar',
        'coverage': ' | '.join(SRC_COVERAGE),
        'class': ' | '.join(SOURCE_CLASSES) + (' (optional)' if en else ' (opcional)'),
        'doc_type': ' | '.join(s[0] for s in SOURCE_TYPES),
    }
    kind_fmt = {'year': 'YYYY' if en else 'AAAA',
                'date': 'YYYY-MM-DD' if en else 'AAAA-MM-DD',
                'lines': 'several separated by ;' if en else 'varios separados por ;',
                'url': 'https://…'}
    labels, formats = [], []
    for c in cols:
        labels.append(_src_col_label(c, lang))
        meta = SRC_FIELD_META.get(c)
        formats.append(fmt.get(c) or (meta and kind_fmt.get(meta[4], '')) or '')
    labels[0] = '# ' + labels[0]
    formats[0] = '# ' + formats[0]
    starter = [entity_id] + [''] * (len(cols) - 1)
    return _src_csv_response([cols, labels, formats, starter], '{}_fuentes.csv'.format(entity_id))


def _src_guide():
    """Which fields each type requires (R) or allows (O), in the viewer's language."""
    lang = resolve_locale()
    en = lang == 'en'
    head = ['field', 'label'] + [s[0] for s in SOURCE_TYPES]
    names = ['', ''] + [s[3] if en else s[2] for s in SOURCE_TYPES]
    names[0] = '# ' + ('kind of resource' if en else 'clase de recurso')
    rows = [head, names]
    for fld, req, opt in _src_matrix():
        rows.append([fld, _src_col_label(fld, lang)] +
                    ['R' if s[0] in req else ('O' if s[0] in opt else '') for s in SOURCE_TYPES])
    return _src_csv_response(rows, 'fuentes_campos_por_tipo.csv' if not en
                             else 'sources_fields_by_type.csv')


def _src_bulk_parse(entity_id, storage, added_by):
    """Return (records, None) or (None, error); error is a code string or a
    (row_number, problem, field) tuple for a row that failed validation."""
    raw = storage.read()
    if not raw or len(raw) > _SRC_BULK_MAX_BYTES:
        return None, 'badfile'
    try:
        text = raw.decode('utf-8-sig')
    except UnicodeDecodeError:
        text = raw.decode('cp1252', errors='replace')
    first = text.split('\n', 1)[0]
    delim = ';' if ';' in first else (',' if ',' in first else '\t')
    rows = list(csv.reader(io.StringIO(text), delimiter=delim))
    if not rows:
        return None, 'badfile'
    head = [h.strip() for h in rows[0]]
    known = set(_src_bulk_columns())
    if (not set(SRC_BULK_LEAD) <= set(head)
            or any(h and h not in known and h not in _SRC_BULK_IGNORED for h in head)):
        return None, 'badcols'

    out = []
    for n, r in enumerate(rows[1:], start=2):     # n = spreadsheet row number
        vals = dict(zip(head, (v.replace('\r\n', '\n') for v in r)))
        if (vals.get(head[0]) or '').strip().startswith('#'):
            continue                               # template guidance row
        content = {k: v for k, v in vals.items()
                   if k not in ('entity_id',) and k not in _SRC_BULK_IGNORED and (v or '').strip()}
        if not content:
            continue                               # empty (or id-only starter) row
        eid = (vals.get('entity_id') or '').strip()
        if eid and eid != entity_id:
            return None, (n, 'entity', 'entity_id')
        rec, err = _src_record(vals.get('class'), vals.get('doc_type'), vals.get('coverage'),
                               lambda f: vals.get(f, ''), added_by, bulk=True)
        if rec is None:
            return None, (n,) + err
        out.append(rec)
        if len(out) > _SRC_BULK_MAX_ROWS:
            return None, 'toomany'
    if not out:
        return None, 'empty'
    return out, None


def _src_bulk_handle(scope, entity_id):
    ent, ent_name = _src_entity(scope, entity_id)
    if ent is None:
        abort(404)
    if request.method == 'GET':
        lang = resolve_locale()
        err_field = request.args.get('f', '')
        return render_template(
            'src_bulk_form.html', scope=scope, entity_id=entity_id, entity_name=ent_name,
            single_kind='pl_source' if scope == 'place' else 'tr_source',
            max_rows=_SRC_BULK_MAX_ROWS, msg=request.args.get('msg', ''),
            err_row=request.args.get('row', ''), err_problem=request.args.get('p', ''),
            err_field=_src_col_label(err_field, lang) if err_field in _src_bulk_columns() else '',
            done_n=request.args.get('n', ''),
            challenge=make_challenge(), turnstile_sitekey=turnstile_site_key())

    endpoint = 'enciclopedia.{}_sources_bulk'.format(scope)

    def _done(code, **extra):
        return redirect(url_for(endpoint, entity_id=entity_id, msg=code, **extra))

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
    for val in (name, comment):
        if val and _BAD_TEXT.search(val):
            return _done('invalid')
    storage = request.files.get('listfile')
    if storage is None or not storage.filename:
        return _done('nofile')
    recs, err = _src_bulk_parse(entity_id, storage, name)
    if isinstance(err, tuple):
        row, problem, field = err
        return _done('rowerr', row=row, p=problem, f=field)
    if err:
        return _done(err)
    if _over_send_cap():
        return _done('embargo')
    ids = _src_store(recs, name, email, comment, entity_id, ent_name, scope, storage.filename)
    if not ids:
        return _done('senderror')
    _record_ip()
    _record_send()
    return _done('thanks', n=len(ids))


@bp.route('/place/<entity_id>/fuentes-plantilla')
def place_sources_template(entity_id):
    if _src_entity('place', entity_id)[0] is None:
        abort(404)
    return _src_template(entity_id)


@bp.route('/territory/<entity_id>/fuentes-plantilla')
def territory_sources_template(entity_id):
    if _src_entity('territory', entity_id)[0] is None:
        abort(404)
    return _src_template(entity_id)


@bp.route('/fuentes-campos-por-tipo')
def sources_field_guide():
    return _src_guide()


@bp.route('/place/<entity_id>/fuentes-lista', methods=['GET', 'POST'])
def place_sources_bulk(entity_id):
    return _src_bulk_handle('place', entity_id)


@bp.route('/territory/<entity_id>/fuentes-lista', methods=['GET', 'POST'])
def territory_sources_bulk(entity_id):
    return _src_bulk_handle('territory', entity_id)


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
