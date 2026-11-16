"""Convert data/fuentes_legacy.csv (legacy Joomla/DokuWiki source list) into the RIS
source template defined in data/guidebooks/ris_source_fields_by_type.csv.

    py -3 transform/fuentes_to_ris.py            (run from apps_as_factory/)

Writes data/fuentes_converted.csv (';'-delimited, UTF-8 with BOM, like the
stored suggestion table). data/fuentes.csv is the maintained table the app
reads (it started as this script's output, then was edited by hand), so the
script never overwrites it: compare or copy the output over deliberately.

Per row the script
  * maps citlist/tipo onto class + doc_type (+ ris_type from the Types sheet),
  * splits conflated legacy fields (en_ -> container/volume/issue/pages/editor/
    place/year; Año ranges -> year + date_text; Autores -> author/editor ...),
  * fills gaps from Cita_completa and from a small table of well-known
    compilations (KNOWN_CONTAINERS) and per-row corrections (OVERRIDES),
  * records which template fields are filled (fields_supplied), which of those
    were derived rather than copied (fields_added), required fields still empty
    (missing_required), fields filled although the matrix does not foresee them
    for the type (fields_outside_matrix), and a review flag + note.
"""
import csv
import html
import os
import re
import sys
import unicodedata
from concurrent.futures import ThreadPoolExecutor

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))     # apps_as_factory
DATA = os.path.join(BASE, 'data')
SRC = os.path.join(DATA, 'fuentes_legacy.csv')
OUT = os.path.join(DATA, 'fuentes_converted.csv')
MATRIX = os.path.join(DATA, 'guidebooks', 'ris_source_fields_by_type.csv')
TYPES = os.path.join(DATA, 'guidebooks', 'ris_source_types.csv')
FETCH_TITLES = '--no-fetch' not in sys.argv

TEMPLATE = ['fuenteID', 'class', 'doc_type', 'ris_type', 'author', 'title', 'year', 'date',
            'date_text', 'editor', 'translator', 'short_title', 'container_title',
            'series_title', 'volume', 'num_volumes', 'issue', 'edition', 'place', 'publisher',
            'page_start', 'page_end', 'scale', 'archive', 'shelfmark', 'original_pub',
            'language', 'isbn_issn', 'doi', 'url', 'accessed', 'keywords', 'notes']
AUTO = {'fuenteID', 'class', 'doc_type', 'ris_type'}
EXTRA = ['FuenteID', 'listed', 'duplicate_of', 'tiempo', 'flag', 'wikiincluded',
         'legacy_type', 'Cita_completa', 'fields_supplied', 'fields_added',
         'missing_required', 'fields_outside_matrix', 'review', 'review_note']

MATRIX_COLS = {
    'Arch. series': 'Archival series/holding', 'Arch. document': 'Archival document',
    'Hist. map': 'Historical map (before 1824)', 'Print': 'Print',
    'Critical edition': 'Critical edition', 'Modern map': 'Modern map (after 1824)',
    'Finding aid': 'Archival catalogue/finding aid', 'Monograph': 'Monograph',
    'Edited volume': 'Edited volume',
    'Chapter': 'Chapter', 'Article': 'Article', 'Thesis': 'Thesis',
    'Scholarly online': 'Scholarly online source', 'Database': 'Database',
    'Other online': 'Other online source',
}
HIST_MAP, MOD_MAP = 'Historical map (before 1824)', 'Modern map (after 1824)'
ARCH_DOC, PRINT, CRIT = 'Archival document', 'Print', 'Critical edition'
FINDING, MONO, CHAP, ART = 'Archival catalogue/finding aid', 'Monograph', 'Chapter', 'Article'
EDVOL = 'Edited volume'
ARCH_SER = 'Archival series/holding'

# An "archival document" that really describes a run of records: a collective
# title (Títulos de corregidores, Cuentas, Duplicados ...) or a legajo range.
SERIES_TITLE_RX = re.compile(
    r'^(t[íi]tulos|provisiones|copias reales provisiones|duplicados|correspondencia|secci[óo]n)\b'
    r'|(^|\.\s*)cuentas\b', re.I)
SERIES_SHELF_RX = re.compile(r'^[^,]+,\s*\d+\s*-\s*\d+$')


def is_series(o):
    title = unicodedata.normalize('NFC', (o.get('title') or '').strip())
    shelf = (o.get('shelfmark') or '').strip()
    return bool(SERIES_TITLE_RX.search(title) or (shelf and SERIES_SHELF_RX.match(shelf)))


THES, ONL_S, DBASE, ONL_O = 'Thesis', 'Scholarly online source', 'Database', 'Other online source'


def load_types():
    with open(TYPES, encoding='utf-8-sig', newline='') as f:
        return {r['doc_type'].strip(): (r['class'].strip(), r['ris_type'].strip())
                for r in csv.DictReader(f)}


def load_matrix():
    req, opt = {}, {}
    with open(MATRIX, encoding='utf-8-sig', newline='') as f:
        for r in csv.DictReader(f):
            for col, dt in MATRIX_COLS.items():
                v = (r.get(col) or '').strip()
                if v == 'R':
                    req.setdefault(dt, set()).add(r['Field'])
                elif v == 'O':
                    opt.setdefault(dt, set()).add(r['Field'])
    return req, opt


# ---------------------------------------------------------------- helpers ----
def clean(s):
    """Excel CR artefacts, non-breaking spaces, runs of white space."""
    s = (s or '').replace('_x000d_', '\n').replace(' ', ' ').replace('\t', ' ')
    s = re.sub(r'_x00[0-9a-fA-F]{2}_', '', s)          # other Excel control-char escapes
    return unicodedata.normalize('NFC', ' '.join(s.split()).strip())


def first_line(s):
    s = (s or '').replace('_x000d_', '\n')
    return clean(s.split('\n')[0])


def strip_end(s):
    return (s or '').strip().rstrip('.,;: ').strip()


PARTICLES = {'de', 'del', 'la', 'las', 'los', 'y', 'da', 'do', 'dos', 'das', 'von', 'van',
             'der', 'den', 'du', 'des', 'i', 'e'}


def tc(s):
    """Title-case a word written in capitals (LUJÁN MUÑOZ -> Luján Muñoz)."""
    if s.isupper() and len(s) > 1:
        return ' '.join(w if w.lower() in PARTICLES else w.capitalize() for w in s.split())
    return s


def sf(name):
    """'Forename Surname' -> 'Surname, Forename'. Returns (value, certain)."""
    name = clean(name).strip(' ,;')
    if not name:
        return '', True
    if ',' in name:                                    # already "Surname, Forename"
        sur, fore = [x.strip() for x in name.split(',', 1)]
        return '{}, {}'.format(tc(sur), fore) if fore else tc(sur), True
    toks = name.split()
    if len(toks) == 1:
        return tc(name), True
    if toks[0].isupper() and len(toks[0]) > 2 and not toks[-1].isupper():
        # "ARELLANO MORENO Antonio": capitals mark the surname
        i = max(k for k, t in enumerate(toks) if t.isupper()) + 1
        return '{}, {}'.format(tc(' '.join(toks[:i])), ' '.join(toks[i:])), True
    # forename block = leading tokens; particles stay with the forename
    core = [t for t in toks if t.lower() not in PARTICLES]
    if len(core) == 2:
        n_fore = 1
    elif len(core) == 3:
        # "Charles W. Polzer" (initial) vs. Hispanic "Gabriel Cabrera Becerra"
        n_fore = 2 if re.fullmatch(r'[A-Z]\.', core[1]) else 1
    elif len(core) == 4:
        n_fore = 2
    else:
        n_fore = 1
    cnt, cut = 0, 0
    for k, t in enumerate(toks):
        if t.lower() not in PARTICLES:
            cnt += 1
            if cnt == n_fore:
                cut = k + 1
                break
    while cut < len(toks) and toks[cut].lower() in PARTICLES:
        cut += 1                                       # Francisco de | Viedma
    fore, sur = ' '.join(toks[:cut]), ' '.join(toks[cut:])
    if not sur:
        return name, False
    initial = len(core) == 3 and n_fore == 2
    return '{}, {}'.format(sur, fore), len(core) in (2, 4) or (len(core) == 3 and n_fore == 1) or initial


ROLE_RX = re.compile(r'\s*\((?:eds?|comps?|coords?|coordinadores|editores|compiladores)\.?\)\s*',
                     re.I)


def split_people(s):
    """'A, B y C' / 'A/B' / 'A; B' -> list of names (role markers removed)."""
    s = ROLE_RX.sub(' ', clean(s))
    s = re.sub(r'\bet al\.?', '', s)
    parts = re.split(r'\s*/\s*|\s*;\s*|\s+y\s+|\s+and\s+|\s*&\s*|\s{2,}|,\s*(?=[A-ZÁÉÍÓÚÑ][a-záéíóúñ]+\s+[A-ZÁÉÍÓÚÑ])',
                     s)
    return [p.strip(' ,.') for p in parts if p.strip(' ,.')]


def people_sf(s):
    out, sure = [], True
    for p in split_people(s):
        v, ok = sf(p)
        out.append(v)
        sure &= ok
    return '; '.join(out), sure


def parse_year(s):
    """-> (year, date_text) from '1786-1789', '1715, ca.', 'c. 1750', '1777'."""
    s = clean(s)
    if not s:
        return '', ''
    if re.fullmatch(r's\.?\s*/?\s*f\.?', s, re.I):
        return '', 's.f.'
    m = re.fullmatch(r'(\d{4})', s)
    if m:
        return s, ''
    m = re.fullmatch(r'(\d{4})\s*,?\s*(?:ca|c)\.?', s, re.I) or \
        re.fullmatch(r'(?:ca|c)\.?\s*(\d{4})', s, re.I)
    if m:
        return m.group(1), 'ca. ' + m.group(1)
    m = re.fullmatch(r'(\d{4})\s*[-–/]\s*(\d{2,4})', s)
    if m:
        b = m.group(2)
        b = m.group(1)[:4 - len(b)] + b
        return m.group(1), '{}–{}'.format(m.group(1), b)
    m = re.search(r'(\d{4})', s)
    return (m.group(1), s) if m else ('', s)


PAGES_RX = re.compile(r'(?:,|:|\s)\s*(?:pp?\.?|p,)?\s*(\d+)\s*[-–]\s*(\d+)\s*\.?\s*$')
PLACEYEAR_RX = re.compile(r'\((?:([^()]*?)[\s,]+)?(\d{4})(?:\s*[-–]\s*\d{2,4})?\)\s*')


def take_pages(s):
    s = re.sub(r'\s*\((\d{4})\)\s*$', '', s) if PAGES_RX.search(
        re.sub(r'\s*\((\d{4})\)\s*$', '', s)) else s      # "…, pp. 1-9 (2015)"
    m = re.search(r'(?:,|:|\s)\s*pp?\.\s*(\d+)\s*ff\.?\s*$', s)
    if m:
        return s[:m.start()].rstrip(' ,.:;'), m.group(1), ''
    m = PAGES_RX.search(s)
    if m and re.search(r'(?:\bno|n[úu]m|nº|n°|\bn)\.?\s*$', s[:m.start() + 1].rstrip(), re.I) \
            and not re.search(r'pp?\.', m.group(0)):
        return s, '', ''                                # "No. 258-260" is an issue range
    if m:
        return s[:m.start()].rstrip(' ,.:;'), m.group(1), m.group(2)
    return s, '', ''


ROMAN = r'(?:[IVXLC]+)'
JOURNAL_WORDS = re.compile(
    r'\b(revista|journal|bolet[ií]n|bulletin|anuario|anales|anu\.|quarterly|cuadernos|'
    r'historia mexicana|histórica|historias|relaciones|andes|memoria americana|'
    r'estudios|letras hist|secuencia|chronica|scripta|desarrollo econ|mesoam|'
    r'folia|clio|apuntes|academvs|baluarte|jahrbuch|tempus|ihs|histo?relo|'
    r'chungar|gazeta|regi[oó]n y sociedad|cultura y representaciones|biota|'
    r'castillos de espa|espacio regional|jangwa|rivar|investigaciones y ensayos|'
    r'mundo agrario|fronteras de la historia|trimestre|equilibrio|historia cr[ií]tica|'
    r'historia caribe|historia y sociedad|historia y memoria|tipit|contrato|'
    r'philatelist|atalaya|passagen|ancient mesoamerica|memorias\. revista|'
    r'el taller de historia|cespedesia|mercurio|estudios de historia novohispana|'
    r'proceso)', re.I)
EDITOR_RX = re.compile(r'\((?:eds?|comps?|coords?)\.?\)|\bet al\. \(eds', re.I)


def parse_journal_ref(s):
    """'Histórica 20, 1 (Lima 1996), pp. 1-28' -> dict(container, volume, issue,
    year, place, page_start, page_end). 'ok' tells whether it parsed cleanly."""
    s = clean(s).rstrip('. ')
    d = {'container_title': '', 'volume': '', 'issue': '', 'year': '', 'place': '',
         'page_start': '', 'page_end': '', 'url': '', 'ok': False}
    m = re.search(r'[:\s]*(https?://\S+)\s*$', s)
    if m:
        d['url'] = m.group(1)
        s = s[:m.start()].rstrip(' :,')
    m = re.search(r',?\s*(\d+)\s*pp\.?$', s)            # "…, 23pp" = extent, not pages
    if m:
        d['extent'] = m.group(1)
        s = s[:m.start()].rstrip(' ,')
    s, d['page_start'], d['page_end'] = take_pages(s)
    m = None
    for m in PLACEYEAR_RX.finditer(s):
        pass
    if m:
        d['place'] = strip_end(m.group(1) or '')
        d['year'] = m.group(2)
        s = (s[:m.start()] + ' ' + s[m.end():]).strip(' ,.:;')
    if not d['year']:
        m2 = re.search(r',\s*(?:[a-z]+(?:-[a-z]+)?\s*(?:de\s*)?)?(\d{4})\b', s, re.I)
        if m2:
            d['year'] = m2.group(1)
            s = (s[:m2.start()] + s[m2.end():]).strip(' ,.')
    if d['place'] and re.search(r'\d', d['place']) or d['place'] in ('Primer Semestre',):
        d['place'] = ''
    if re.search(r'[a-z]', d['place'] or '') and len(d['place']) > 25:
        d['place'] = ''
    # Journal VOL, ISSUE / vol. X, núm. Y / X (Y) / X: Y
    rx = re.compile(
        r'^(?P<c>.*?)[\s,.:;]+(?:vol(?:ume|umen)?\.?\s*|v\.|tomo\s*|año\s+\S+,?\s*vol\.?\s*)?'
        r'(?P<v>' + ROMAN + r'|\d+(?:-\d+)?)'
        r'(?:\s*[,(;:\-–]?\s*(?:n[úu]m(?:ero)?\.?|no\.?|n\.|nº|n°|issue|cuadernos)?\s*'
        r'(?P<i>' + ROMAN + r'|\d+(?:\s*[-–/]\s*\d+)?)\)?(?:\s*\(\d+\))?)?$', re.I)
    m = rx.match(s)
    if m and m.group('c'):
        d.update(container_title=strip_end(m.group('c')), volume=m.group('v'),
                 issue=(m.group('i') or '').replace(' ', ''), ok=True)
    else:
        m = re.match(r'^(?P<c>.*?)[\s,]+(?:n[úu]m(?:ero)?\.?|no\.?|nº|n°)\s*(?P<i>\d+)$', s, re.I)
        if m:
            d.update(container_title=strip_end(m.group('c')), issue=m.group('i'), ok=True)
        else:
            d['container_title'] = strip_end(s)
            d['ok'] = not re.search(r'\d', s)           # plain title, nothing left to split
    d['container_title'] = re.sub(r'\s+vol\.?$', '', d['container_title'], flags=re.I)
    return d


def parse_book_ref(s):
    """'Florescano/Gil Sánchez (eds.), Title, tomo II (México 1976)' or
    'ARELLANO MORENO, Antonio: Title (Caracas 1964)' or 'Eidenbach, Peter. Title
    (Albuquerque 2012)' -> dict(editor|author, title, volume, place, year, pages)."""
    s = clean(s).rstrip('. ')
    d = {'people': '', 'role': '', 'title': '', 'volume': '', 'num_volumes': '',
         'place': '', 'year': '', 'page_start': '', 'page_end': '', 'ok': False}
    s, d['page_start'], d['page_end'] = take_pages(s)
    m = re.search(r',\s*(?:vol\.?|v\.)\s*([\dIVX]+(?:,\s*\d+)?)\s*$', s)    # trailing ', vol. 2'
    if m:
        d['volume'] = m.group(1)
        s = s[:m.start()]
    m = None
    for m in PLACEYEAR_RX.finditer(s):
        pass
    if m:
        d['place'] = strip_end(m.group(1) or '')
        d['year'] = m.group(2)
        s = (s[:m.start()] + s[m.end():]).strip(' ,.:;')
    m = re.search(r',\s*(\d+)\s*vols?\.?,?\s*$', s)
    if m:
        d['num_volumes'] = m.group(1)
        s = s[:m.start()]
    m = re.search(r',\s*(?:tomo|vol\.?|volumen)\s*([IVXLC]+|\d+)\s*$', s, re.I)
    if m:
        d['volume'] = m.group(1)
        s = s[:m.start()]
    m = EDITOR_RX.search(s)
    if m:                                               # People (eds.), Title
        d['people'], d['role'] = s[:m.start()].strip(' ,'), 'editor'
        d['title'] = s[m.end():].strip(' ,.:')
        d['ok'] = True
    else:
        m = re.match(r'^([A-ZÁÉÍÓÚÑ][^:]{2,60}?)\s*:\s*(.+)$', s)      # SURNAME, Forename: Title
        m2 = re.match(r'^([A-ZÁÉÍÓÚÑ][\w\-]+(?: [\w\-]+)?, [A-ZÁÉÍÓÚ][\w.]+(?: [\w.]+)?)\.\s+(.+)$', s)
        if m and (',' in m.group(1) or m.group(1).isupper() or '/' in m.group(1)):
            d['people'], d['role'], d['title'], d['ok'] = m.group(1), 'author', m.group(2), True
        elif m2:
            d['people'], d['role'], d['title'], d['ok'] = m2.group(1), 'author', m2.group(2), True
        else:
            d['title'] = s
            d['ok'] = bool(d['year'])
    d['title'] = strip_end(d['title'])
    return d


def people_from_ref(p):
    """'FLORESCANO, Enrique/Isabel Gil' or 'Lydia Espinosa Morales/Isabel Ortega'."""
    out, sure = [], True
    for part in re.split(r'\s*/\s*|\s*;\s*|\s+y\s+|\s{2,}', clean(p)):
        part = part.strip(' ,.')
        if not part:
            continue
        if ',' in part and part.split(',')[0].strip().isupper():
            v, ok = sf(part)
        elif ',' in part and len(part.split(',')) > 2:
            sub = [sf(x) for x in part.split(',') if x.strip()]
            out += [v for v, _ in sub]
            sure &= all(ok for _, ok in sub)
            continue
        elif ',' in part and len(part.split(',')[1].split()) >= 2:   # "A B, C D" = two people
            sub = [sf(x) for x in part.split(',') if x.strip()]
            out += [v for v, _ in sub]
            sure &= all(ok for _, ok in sub)
            continue
        else:
            v, ok = sf(part)
        out.append(v)
        sure &= ok
    return '; '.join(out), sure


# Archive -> (city, normalized name)
ARCHIVES = {
    'archivo general de indias': ('Sevilla', 'Archivo General de Indias'),
    'archivo general de la nación (colombia)': ('Bogotá', 'Archivo General de la Nación (Colombia)'),
    'archivo general de la nación (méxico)': ('Ciudad de México', 'Archivo General de la Nación (México)'),
    'museo naval de madrid': ('Madrid', 'Museo Naval de Madrid'),
    'biblioteca nacional de españa': ('Madrid', 'Biblioteca Nacional de España'),
    'archivo general de simancas': ('Simancas', 'Archivo General de Simancas'),
    'real academia de historia': ('Madrid', 'Real Academia de la Historia'),
    'real academia de la historia': ('Madrid', 'Real Academia de la Historia'),
    'library of congress': ('Washington, D.C.', 'Library of Congress'),
    'mapoteca manuel orozco y berra': ('Ciudad de México', 'Mapoteca Manuel Orozco y Berra'),
    'new york public library': ('New York', 'New York Public Library'),
    'archivo histórico municipal de parral': ('Hidalgo del Parral', 'Archivo Histórico Municipal de Parral'),
    'archivo histórico municipal de parral,': ('Hidalgo del Parral', 'Archivo Histórico Municipal de Parral'),
    'bexar archives online': ('Austin', 'Bexar Archives (Dolph Briscoe Center for American History)'),
    'archivo general de la nación (perú)': ('Lima', 'Archivo General de la Nación (Perú)'),
    'archivo general de la nación (argentina)': ('Buenos Aires', 'Archivo General de la Nación (Argentina)'),
    'archivo nacional de chile': ('Santiago de Chile', 'Archivo Nacional de Chile'),
    'archivo histórico nacional': ('Madrid', 'Archivo Histórico Nacional'),
    'archivo nacional de asunción': ('Asunción', 'Archivo Nacional de Asunción'),
    'archivo nacional del ecuador': ('Quito', 'Archivo Nacional del Ecuador'),
    'british library': ('London', 'British Library'),
    'archivo general de centro américa': ('Guatemala', 'Archivo General de Centro América'),
    'archivo general de la nación (venezuela)': ('Caracas', 'Archivo General de la Nación (Venezuela)'),
    'biblioteca nacional de chile': ('Santiago de Chile', 'Biblioteca Nacional de Chile'),
    'servicio geográfico del ejército': ('Madrid', 'Servicio Geográfico del Ejército'),
    'archivo general militar de madrid': ('Madrid', 'Archivo General Militar de Madrid'),
    'archivo del museo naval': ('Madrid', 'Museo Naval de Madrid'),
    'biblioteca nacional del perú': ('Lima', 'Biblioteca Nacional del Perú'),
    'archivo nacional de bolivia': ('Sucre', 'Archivo y Biblioteca Nacionales de Bolivia'),
    'archivo y biblioteca nacionales de bolivia': ('Sucre', 'Archivo y Biblioteca Nacionales de Bolivia'),
    'archivo nacional de cuba': ('La Habana', 'Archivo Nacional de Cuba'),
    'archivo nacional de historia (ecuador)': ('Quito', 'Archivo Nacional de Historia (Ecuador)'),
    'archivo general de la nación (república dominicana)': ('Santo Domingo', 'Archivo General de la Nación (República Dominicana)'),
    'the john carter brown library': ('Providence', 'John Carter Brown Library'),
    'john carter brown library': ('Providence', 'John Carter Brown Library'),
    'bibliothèque nationale de france': ('Paris', 'Bibliothèque nationale de France'),
    'archivo histórico de la provincia de buenos aires': ('La Plata', 'Archivo Histórico de la Provincia de Buenos Aires'),
    'archivo histórico cipriano rodríguez santa maría': ('Chía', 'Archivo Histórico Cipriano Rodríguez Santa María'),
    'biblioteca nacional de colombia': ('Bogotá', 'Biblioteca Nacional de Colombia'),
    'colección edward e. ayer': ('Chicago', 'Newberry Library, Edward E. Ayer Collection'),
    'archivo histórico de guayaquil': ('Guayaquil', 'Archivo Histórico de Guayaquil'),
    'archivo nacional de paraguay': ('Asunción', 'Archivo Nacional de Asunción'),
    'biblioteca di bologna': ('Bologna', 'Biblioteca di Bologna'),
    'cartoteca del centro geográfico del ejército': ('Madrid', 'Centro Geográfico del Ejército'),
    'centro geográfico del ejército': ('Madrid', 'Centro Geográfico del Ejército'),
}

# Recurring compilations (container of edited documents / reproduced maps).
# Facts added from general bibliographic knowledge are listed in 'added'.
KNOWN_CONTAINERS = [
    (r'Descripciones económicas regionales de Nueva España',
     dict(editor='Florescano, Enrique; Gil Sánchez, Isabel',
          container_title='Descripciones económicas regionales de Nueva España: provincias del Norte / del Centro, Sudeste y Sur, 1790-1814',
          place='México', year='1976', publisher='SEP-INAH'),
     ['publisher'], 'publisher SEP-INAH and full title from bibliographic knowledge — verify'),
    (r'Descripciones económicas generales de Nueva España',
     dict(editor='Florescano, Enrique; Gil Sánchez, Isabel',
          container_title='Descripciones económicas generales de Nueva España, 1784-1817',
          place='México', year='1973', publisher='SEP-INAH'),
     ['publisher'], ''),
    (r'Relaciones histórico-geográficas de la Audiencia de Quito',
     dict(editor='Ponce Leiva, Pilar',
          container_title='Relaciones histórico-geográficas de la Audiencia de Quito (siglos XVI-XIX)',
          num_volumes='2', place='Madrid', year='1991', publisher='CSIC'),
     ['publisher', 'editor'], ''),
    (r'Relaciones geográficas de Venezuela',
     dict(editor='Arellano Moreno, Antonio', container_title='Relaciones geográficas de Venezuela',
          place='Caracas', year='1964', publisher='Academia Nacional de la Historia'),
     ['publisher'], ''),
    (r'Relaciones geográficas e históricas del siglo XVIII del reino de Guatemala',
     dict(editor='Luján Muñoz, Jorge',
          container_title='Relaciones geográficas e históricas del siglo XVIII del reino de Guatemala: Relaciones geográficas e históricas de la década de 1740',
          place='Guatemala', year='2006', publisher='Universidad del Valle de Guatemala'),
     ['publisher'], 'publisher (Universidad del Valle de Guatemala) from bibliographic knowledge — verify'),
    (r'Relaciones geográficas de la Nueva Granada',
     dict(container_title='Relaciones geográficas de la Nueva Granada (siglos XVI a XIX)',
          series_title='Cespedesia 45-46, suplemento 4', place='Cali', year='1983',
          publisher='INCIVA'),
     ['publisher'], 'published as Cespedesia 45-46, supl. 4; editor (V. M. Patiño?) not confirmed'),
    (r'NUEVO REINO DE LEÓN EN VOZ DE SUS CONTEMPORÁNEOS',
     dict(editor='Espinosa Morales, Lydia; Ortega Ridaura, Isabel',
          container_title='El Nuevo Reino de León en voz de sus contemporáneos', place='México',
          year='2006'),
     [], ''),
    (r'Presidio and Militia on the Northern Frontier',
     dict(editor='Naylor, Thomas H.; Polzer, Charles W.',
          container_title='The Presidio and Militia on the Northern Frontier of New Spain: A Documentary History',
          place='Tucson', year='1997', publisher='University of Arizona Press'),
     ['publisher', 'place'],
     'Naylor/Polzer edited vol. 1 (1986); the 1997 vol. 2 parts have other editors (Hadley/Naylor/Schuetz-Miller; Polzer/Sheridan) — check which part'),
    (r'Materiales para la historia de Sonora',
     dict(editor='Pérez Taylor, Rafael; Paz Frayre, Miguel Ángel',
          container_title='Materiales para la historia de Sonora', volume='I', place='México',
          year='2007', publisher='UNAM'),
     ['publisher'], ''),
    (r'Conocer para gobernar',
     dict(editor='Escobedo Delgado, Martín; Cuesta Alonso, Marcelino',
          container_title='Conocer para gobernar. Estadísticas de la Intendencia de Zacatecas, 1793-1820',
          place='Oviedo', year='2014'),
     [], ''),
    (r'Descripcion del reyno de Santa Fe de Bogota',
     dict(container_title='Descripción del Reyno de Santa Fe de Bogotá', place='Bogotá', year='1950'),
     [], ''),
    (r'Relación histórica del viage que hizo a los Reynos del Perú',
     dict(editor='Jaramillo-Arango, Jaime',
          container_title='Relación histórica del viage que hizo a los Reynos del Perú y Chile el botánico D. Hipólito Ruiz en el año de 1777 hasta el de 1788',
          num_volumes='2', volume='2', place='Madrid', year='1952'),
     ['editor'], 'editor J. Jaramillo-Arango from bibliographic knowledge — verify'),
    (r'Atlas of Historic New Mexico Maps',
     dict(container_title='Atlas of Historic New Mexico Maps, 1550-1941', place='Albuquerque',
          year='2012', publisher='University of New Mexico Press',
          _container_author='Eidenbach, Peter L.'),
     ['publisher'], ''),
    (r'Cartografía jesuítica del R[ií]o de la Plata',
     dict(container_title='Cartografía jesuítica del Río de la Plata', place='Buenos Aires',
          year='1936', _container_author='Furlong, Guillermo'),
     ['place'], ''),
    (r'^Trujillo del Perú',
     dict(container_title='Trujillo del Perú', volume='I', place='Madrid', year='1978',
          publisher='Ediciones Cultura Hispánica', _author='Martínez Compañón, Baltasar Jaime'),
     ['author', 'place', 'publisher'],
     'author (bishop Martínez Compañón, ca. 1782-1790) and facsimile edition (Madrid 1978) from bibliographic knowledge — verify'),
    (r'Subdelegaciones Novohispanas',
     dict(editor='Diego-Fernández Sotelo, Rafael; Bernal Ruiz, Graciela; Alcauter Guzmán, José Luis',
          container_title='Subdelegaciones novohispanas. La jurisdicción como territorio y competencia',
          place='Zamora, Mich.', year='2019'),
     [], ''),
    (r'De reinos y subdelegaciones',
     dict(editor='Diego-Fernández Sotelo, Rafael; Gutiérrez Lorenzo, María Pilar; Arrioja Díaz Viruell, Luis Alberto',
          container_title='De reinos y subdelegaciones. Nuevos escenarios para un nuevo orden en la América borbónica',
          place='Zamora, Mich.', year='2014'),
     ['place'], 'place (Zamora, El Colegio de Michoacán) and forename "Luis Alberto" from bibliographic knowledge; legacy says México — verify'),
    (r'^Mercurio Peruano',
     dict(container_title='Mercurio Peruano', place='Lima',
          publisher='Sociedad Académica de Amantes del País'),
     ['place', 'publisher'], ''),
]

# Per-row corrections for rows the generic rules cannot handle.  'review' marks
# rows where the change rests on interpretation.
OVERRIDES = {
    'kossok': dict(author='Kossok, Manfred',
                   title='El virreinato del Río de la Plata: su estructura económico-social',
                   place='Buenos Aires', year='1959'),
    'barreira': dict(author='Barreira, Darío',
                     title='Corregidores sin corregimientos: un caso de mestizaje institucional en Santa Fe del Río de la Plata durante los siglos XVII y XVIII',
                     container_title='Revista de estudios histórico-jurídicos', volume='36',
                     year='2014', page_start='245', page_end='269'),
    'perez-gallego': dict(author='Pérez Gallego, Francisco Alfonso',
                          title='Los sistemas de baterías en los núcleos fortificados de Venezuela (1700-1830)',
                          container_title='Castillos de España', volume='184', year='2022',
                          page_start='113', page_end='157'),
    'estrano': dict(title='Pueblos mixtos y «de españoles» al sur del Orinoco: la participación de los zambos, mulatos, cimarrones y otras gentes de color en el poblamiento y control territorial de la Guayana española. Segunda mitad del siglo XVIII',
                    container_title='Boletín Antropológico', volume='36', issue='95',
                    year='2018', page_start='165', page_end='192'),
    'frerer-lew': dict(author='Ferrer, Arnaldo; Lew, Daniel; Vispo, Conrad; Daza, Felix',
                       title='Uso de la fauna silvestre y acuática por comunidades del bajo río Caura (Guayana venezolana)',
                       container_title='Biota Colombiana', volume='14', issue='2', year='2013',
                       page_start='33', page_end='44'),
    'luciana-fernandez': dict(url='https://revistas.icanh.gov.co/index.php/fh/article/view/2363/1984',
                              container_title='Fronteras de la Historia', year='2022'),
    'informe-darien-1774': dict(author='Ariza, Antonio de', container_title='Anales de Instrucción Pública',
                                volume='5', page_start='369', page_end='402', place='Bogotá',
                                _added=['place']),
    'tequila-subdelegacion': dict(editor='Jiménez Vizcarra, Claudio; Niemeyer, Katharina; Fernández, Rodolfo; Carrano, Diana',
                                  container_title='Transformaciones socioculturales en México en el contexto de la conquista y colonización',
                                  _review='names before the container taken as editors; "Katrarina" read as Katharina'),
    'boyer': dict(author='Boyer, William',
                  title='Nuestra Señora del Rosario de la Punta: Lifeways of an Eighteenth-Century Colonial Spanish Refugee Mission Community, St. Augustine, Florida',
                  _review='legacy "Online" without URL; probably a thesis — type, year and institution unknown'),
    'mercedarios-peru': dict(author='Aparicio Quispe, Severo',
                             title='Mercedarios: misiones en el Cuzco y en Lima',
                             container_title='Diccionario de Historia de la Iglesia en América Latina (DHIAL)'),
    'ehcial-merc': dict(title='Mercedarios en la evangelización de América',
                        container_title='Diccionario de Historia de la Iglesia en América Latina (DHIAL)'),
    'dbe-amancio-gonzalez': dict(title='Amancio González y Escobar', container_title='Diccionario Biográfico electrónico (DB~e)',
                                 publisher='Real Academia de la Historia'),
    'bing': dict(url='https://www.bing.com/maps', publisher='Microsoft'),
    'osm': dict(title='OpenStreetMap', url='https://www.openstreetmap.org', publisher='OpenStreetMap Foundation'),
    'geonames': dict(title='GeoNames', url='https://www.geonames.org'),
    'tumacori': dict(doc_type=MONO, publisher='National Park Service', place='',
                     notes='Legacy place "s.l."',
                     _review='legacy Online without URL; 1976 NPS publication (brochure?)'),
    'wiki-morelia': dict(url='https://es.wikipedia.org/wiki/Morelia_(Caquet%C3%A1)', container_title='Wikipedia',
                         _review='URL reconstructed from the article title'),
    'pablillo': dict(container_title='Facebook', _review='Facebook page, no URL recorded'),
    'iglesia-valparaiso': dict(volume='2', container_title=''),
    'noli_noejovich': dict(editor='Noejovich, Héctor Omar',
                           container_title='América bajo los Austrias: economía, cultura y sociedad'),
    'pedrero-nieto': dict(editor='Márquez Espinosa, Esaú; Araujo González, Rafael de J.; Ortiz Herrera, María del Rocío',
                          container_title='Estado-Nación en México: Independencia y Revolución'),
    'martinez-romero': dict(title='Población, gobierno y conflictos en los pueblos de indios de San Sebastián Agua del Venado y San Jerónimo de Agua Hedionda: 1679-1767',
                            notes='Legacy title ends "pp. 67-71"',
                            _review='title had "pp. 67-71" appended (part of a larger work?); year/place/publisher unknown'),
    'nectario-maria': dict(author='Nectario María, Hermano'),
    'worth-harris': dict(title='San Joseph de Escambe: An 18th-Century Apalachee Mission in the West Florida Borderlands',
                         container_title='Annual Conference of the Society for Historical Archaeology',
                         place='Austin, Texas', year='2011',
                         notes='Paper presented at the 2011 Conference of the Society for Historical Archaeology, Austin, Texas'),
    'hoffman': dict(doc_type=MONO, container_title='', series_title='Documento de trabajo 33',
                    publisher='Universidad del Valle, Centro de Investigación y Documentación Socioeconómica (CIDSE)'),
    'sarabia-viejo': dict(place='Sevilla'),
    'rersab-2014': dict(editor='Diego-Fernández Sotelo, Rafael; Gutiérrez Lorenzo, María Pilar; Arrioja Díaz Viruell, Luis Alberto',
                        _added=['editor'],
                        _review='editor names corrected (Arrioja Díaz Viruell) and forename "Luis Alberto" from bibliographic knowledge — verify'),
    'rersab-2019': dict(editor='Diego-Fernández Sotelo, Rafael; Bernal Ruiz, Graciela; Alcauter Guzmán, José Luis',
                        _added=['editor']),
    'mapa-chaco': dict(notes='Reproduced in Furlong, Cartografía jesuítica del Río de la Plata (Buenos Aires 1936), n. XLVIII'),
    'stangl_2': dict(doc_type=ONL_S, container_title='Mundus Alter (losreinosdelasindias.hypotheses.org)',
                     url='http://losreinosdelasindias.hypotheses.org/1109',
                     notes='Blog post in two parts: http://losreinosdelasindias.hypotheses.org/1109 and /1130',
                     _review='blog post; publication year not recorded'),
    'hg-durango-cramaussel': dict(doc_type=CHAP, container_title='Historia de Durango', volume='2',
                                  notes='Vol. 2: La Nueva Vizcaya',
                                  _review='Artículo reclassified as Chapter (Historia de Durango, vol. 2); editor/publisher unknown'),
    'chaile': dict(container_title='Dimensión Antropológica', volume='49', page_start='7',
                   page_end='42', year='2010',
                   _review='legacy pages "75-42" and year 2011 conflict with the pasted journal data (vol. 49, May-Aug 2010, pp. 7-42) — taken from the latter'),
    'casablanca': dict(container_title='El Mercurio de Valparaíso', notes='Newspaper article, 05-12-2005'),
    'battcock': dict(container_title='Cuicuilco', volume='11', issue='30', year='2004',
                     page_start='1', page_end='22'),
    'colon-larriategui': dict(author='Colón de Larriátegui, Félix',
                              title='Juzgados militares de España y sus Indias'),
    'osante': dict(doc_type=MONO, author='Osante, Patricia',
                   title='Orígenes del Nuevo Santander, 1748-1772', place='México', year='1997'),
    'ortega_cavazos': dict(title='Nuevo León. Historia breve'),
    'medinaceli': dict(title='Sariri. Los llameros y la construcción de la sociedad colonial'),
    'caulin': dict(title='Historia corográfica, natural y evangélica de la Nueva Andalucía'),
    'cortes-madariaga': dict(title='Diario y observaciones del presbítero José Cortés Madariaga en su regreso de Santafé a Caracas, por la vía de los ríos Negro, Meta y Orinoco, después de haber concluido la comisión que obtuvo de su Gobierno, para acordar los tratados de alianza entre ambos Estados'),
    'basilio-oviedo': dict(doc_type=CRIT, title='Cualidades y riquezas del Nuevo Reino de Granada',
                           _review='edition (year/publisher/editor) not recorded in legacy data'),
    'zeitschrift-erdkunde-2': dict(doc_type=PRINT, title='Zeitschrift für allgemeine Erdkunde',
                                   series_title='Neue Folge', volume='2', author='',
                                   editor='Neumann, K.',
                                   _review='journal volume as a whole; legacy author K. Neumann taken as editor'),
    'pino_manrique': dict(author='Pino Manrique, Juan del', doc_type=CRIT, original_pub='1787',
                          editor='Angelis, Pedro de',
                          container_title='Colección de obras y documentos relativos a la historia antigua y moderna de las provincias del Río de la Plata',
                          _added=['editor', 'container_title'],
                          _review='Buenos Aires 1836 = Pedro de Angelis, Colección de obras…; editor/container added from knowledge'),
    'tamajuncosa': dict(doc_type=CRIT, editor='Angelis, Pedro de',
                        container_title='Colección de obras y documentos relativos a la historia antigua y moderna de las provincias del Río de la Plata',
                        volume='5'),
    'humboldt': dict(author='Humboldt, Alexander von', doc_type=CRIT, place='', url='https://www.gutenberg.org/ebooks/28280',
                     original_pub='1859',
                     _review='digital (Project Gutenberg) copy of the 1859 German translation; legacy place "Born digital", URL replaced by the Gutenberg link from Cita_completa'),
    'perasso': dict(doc_type=CRIT,
                    title='Descripción (trunca) de la provincia del Paraguay con algunos pueblos de su dependencia, año de 1761',
                    editor='Perasso, José Antonio',
                    container_title='El Paraguay del siglo XVIII en tres memorias',
                    url='http://www.portalguarani.com/951_jose_antonio_perasso/14181_el_paraguay_del_siglo_xviii_en_tres_memorias__por_jose_a_perasso.html',
                    year='', original_pub='1761', _review='edition year/publisher unknown (only online copy)'),
    'alcedo-guayaquil': dict(doc_type=PRINT, place='Madrid',
                             title='Compendio histórico de la provincia, partidos, ciudades, astilleros, ríos y puerto de Guayaquil en las costas de la mar del Sur'),
    'alcedo': dict(doc_type=PRINT),
    'viedma': dict(doc_type=CRIT, original_pub='1793', year='',
                   _review='online copy (Cervantes Virtual); edition used and its year unknown'),
    'barks': dict(doc_type=MONO, title='The Carl Barks Library', publisher='Ehapa',
                  _review='comic anthology, no fitting type; place/year unknown'),
    'listado-huatulco': dict(doc_type=ARCH_DOC, archive='', notes='Unpublished list, personal communication',
                             _review='personal communication by N. O. Vázquez Mendoza; no fitting doc_type (stored as Archival document)'),
    'lucena': dict(doc_type=ART, _review='legacy type Monografía, but en_ is a journal (Revista de Indias LIV, 200)'),
    'rumazo': dict(doc_type=CHAP, _review='legacy type Monografía, en_ looks like a part of an anthology'),
    'ruiz-barriga': dict(publisher='Universidad Michoacana de San Nicolás de Hidalgo'),
    'ultreras': dict(place='San Luis Potosí', publisher='El Colegio de San Luis'),
    'historia-recoletos': dict(volume='IX'),
    'iglesia-valparaiso': dict(volume='2'),
    'cartografia-ultramar': dict(doc_type=MONO,
                                 _review='publisher (Servicio Geográfico / Histórico Militar del Ejército) not confirmed'),
    'furlong': dict(doc_type=MONO, place='Buenos Aires', _added=['place']),
    'maeder-atlas-2': dict(doc_type=MONO),
    'atlas-arzobispado-mexico': dict(doc_type=HIST_MAP, archive='Biblioteca de Castilla-La Mancha',
                                     place='Toledo',
                                     notes='Legacy archive: Biblioteca de Castilla-La Mancha / BPE en Toledo'),
    'humboldt-ensayo': {},
    'chc0347000085': dict(archive='University of Miami Libraries, Cuban Heritage Collection',
                          place='Coral Gables',
                          container_title='Cuban Historical and Literary Manuscript Collection (CHC0347)',
                          shelfmark='CHC0347, Box 3, Folder 28',
                          _review='archive/shelfmark parcelled out of a long legacy archive string'),
    'amat-viaje': dict(shelfmark='bdh0000130499'),
    'maldonado': dict(doc_type=PRINT,
                      _review='legacy Archivalia without archive; URL is a transcription of the printed memorial'),
}

# Domains -> doc_type for online material
DOMAIN_TYPES = [
    (r'viaf\.org|wikidata\.org|catholic-hierarchy|gcatholic|worldstatesmen|familysearch|'
     r'ngdc\.noaa|jvlone\.com|ceresis', DBASE),
    (r'\.edu\b|\.ac\.|hal\.|scielo|redalyc|dialnet|cervantesvirtual|archive\.org|gutenberg|'
     r'jstor|academia\.edu|core\.ac\.uk|dhial\.org|dbe\.rah\.es|revistas\.|journals\.|'
     r'texasbeyondhistory|unlp|historiapolitica|pucsp|bdh-rd\.bne', ONL_S),
]
GEOSERVICE = DBASE

# Well-known web resources: domain -> (title, publisher/responsible institution)
SITE_TITLES = {
    'viaf.org': ('VIAF: The Virtual International Authority File', 'OCLC'),
    'wikidata.org': ('Wikidata', 'Wikimedia Foundation'),
    'catholic-hierarchy.org': ('Catholic-Hierarchy', 'David M. Cheney'),
    'gcatholic.org': ('GCatholic.org', 'Gabriel Chow'),
    'worldstatesmen.org': ('WorldStatesmen.org', 'Ben Cahoon'),
    'dbe.rah.es': ('Diccionario Biográfico electrónico (DB~e)', 'Real Academia de la Historia'),
    'familysearch.org': ('FamilySearch', 'The Church of Jesus Christ of Latter-day Saints'),
    'fallingrain.com': ('Falling Rain Genomics Global Gazetteer', ''),
    'maplandia.com': ('Maplandia', ''),
    'viamichelin.es': ('ViaMichelin', 'Michelin'),
    'wikimapia.org': ('Wikimapia', ''),
    'wikiloc.com': ('Wikiloc', ''),
    'mapcarta.com': ('Mapcarta', ''),
    'mindat.org': ('Mindat.org', 'Hudson Institute of Mineralogy'),
    'nps.gov': ('National Park Service', 'National Park Service'),
    'ecured.cu': ('EcuRed', ''),
    'enciclopediapr.org': ('Enciclopedia de Puerto Rico', 'Fundación Puertorriqueña de las Humanidades'),
    'fortwiki.com': ('FortWiki', ''),
    'northamericanforts.com': ('North American Forts', ''),
    'fortalezas.org': ('Fortalezas.org', ''),
    'es.wikipedia.org': ('Wikipedia, la enciclopedia libre', 'Wikimedia Foundation'),
    'mapasamerica.dices.net': ('Mapas América', ''),
    'viasatelital.com': ('Vía Satelital', ''),
    'pages.uwf.edu': ('John E. Worth (University of West Florida) — personal research pages', 'University of West Florida'),
    'edition-humboldt.de': ('edition humboldt digital', 'Berlin-Brandenburgische Akademie der Wissenschaften'),
}

LANG_WORDS = {
    'es': {'de', 'la', 'el', 'los', 'las', 'y', 'del', 'en', 'una', 'por', 'historia', 'provincia',
           'siglo', 'sobre', 'con', 'para', 'su', 'sus', 'que', 'entre', 'misiones', 'pueblos', 'al'},
    'en': {'the', 'of', 'and', 'in', 'on', 'history', 'spanish', 'a', 'to', 'from', 'among',
           'colonial', 'mission', 'early', 'its'},
    'de': {'die', 'der', 'und', 'im', 'des', 'den', 'zur', 'von', 'über', 'mit', 'reise',
           'geschichte', 'zeitschrift', 'für'},
    'fr': {'les', 'des', 'et', 'du', 'le', 'dans', 'une', 'aux', 'sur', 'histoire', 'langue',
           'société', 'américanistes', 'indiens', 'française', 'études'},
    'pt': {'os', 'das', 'dos', 'em', 'uma', 'no', 'na', 'ao', 'localização', 'história', 'e'},
    'it': {'il', 'gli', 'della', 'delle', 'paesi', 'confinanti', 'e', 'di', 'carta', 'del'},
}


def guess_lang(text):
    words = re.findall(r"[a-záéíóúñüàèìòùçãõâêôäöß]+", (text or '').lower())
    if len(words) < 3:
        return ''
    score = {k: sum(w in v for w in words) for k, v in LANG_WORDS.items()}
    best = max(score, key=score.get)
    ranked = sorted(score.values(), reverse=True)
    if ranked[0] >= 2 and ranked[0] - ranked[1] >= 2:
        return best
    return ''




URL_RX = re.compile(r'^(https?://|www\.)|^[\w-]+(\.[\w-]+)+(/|$)', re.I)


def is_url(s):
    return bool(URL_RX.match((s or '').strip()))


def fix_url(u):
    u = clean(u)
    if not u or ' ' in u or u.lower() in ('secundaria', 'primaria'):
        return ''
    if not re.match(r'https?://', u, re.I):
        u = 'https://' + u.lstrip('/')
    return u


def site_name(u):
    m = re.match(r'https?://(?:www\.)?([^/]+)', u or '')
    return m.group(1) if m else ''


def fetch_title(u):
    import urllib.request
    try:
        req = urllib.request.Request(u, headers={'User-Agent': 'Mozilla/5.0 (source-check)'})
        with urllib.request.urlopen(req, timeout=12) as r:
            raw = r.read(200_000)
            cs = r.headers.get_content_charset() or 'utf-8'
        txt = raw.decode(cs, errors='replace')
        if '�' in txt:
            txt = raw.decode('cp1252', errors='replace')
        m = re.search(r'<title[^>]*>(.*?)</title>', txt, re.I | re.S)
        if m:
            t = clean(html.unescape(m.group(1)))
            t = re.sub(r'\s+[-–—]\s+Wikipedia.*$', '', t)
            if re.search(r'casino|apuestas|\bbet\b|1xbet|domain (is )?for sale|parked', t, re.I):
                return '#REPURPOSED'
            if t and len(t) < 250 and not re.search(r'404|not found|error|forbidden|just a moment', t, re.I):
                return t
    except Exception:
        pass
    return ''


AGI_AUDIENCIAS = {'buenos aires', 'caracas', 'guadalajara', 'guatemala', 'lima', 'mexico',
                  'quito', 'santa fe', 'santo domingo', 'charcas', 'chile', 'panama', 'filipinas',
                  'cuzco'}
AGI_SECTIONS = {'indiferente': 'Indiferente General', 'contaduria': 'Contaduría',
                'contratacion': 'Casa de la Contratación', 'estado': 'Estado',
                'correos': 'Correos', 'cuba': 'Papeles de Cuba', 'ultramar': 'Ultramar'}
AGN_CO_REGION = {'ANT': 'Antioquia', 'BOL': 'Bolívar', 'C/MARCA': 'Cundinamarca', 'VEN': 'Venezuela',
                 'BOY': 'Boyacá', 'SAN': 'Santander', 'VAR': 'Varias', 'DEPTOS': 'Varios Departamentos',
                 'PAN-ECUA-VEN': 'Panamá, Ecuador, Venezuela'}
AGN_CO_FONDO = {'CENSOS': 'Censos', 'POBLACIONES': 'Poblaciones', 'CORREOS': 'Correos',
                'VISITAS': 'Visitas', 'CAPELL': 'Capellanías', 'RES': 'Residencias',
                'RESI': 'Residencias', 'CACIQUES_INDIOS': 'Caciques e Indios',
                'MISCELANEA': 'Miscelánea', 'ASUNTOS-IMPORTANTES': 'Asuntos Importantes',
                'CABILDOS': 'Cabildos', 'ADUANAS': 'Aduanas', 'COMPETENCIAS': 'Competencias',
                'CRIMINALES-JUICIOS': 'Criminales (Juicios)', 'CURAS-OBISPOS': 'Curas y Obispos',
                'QUINAS': 'Quinas', 'TRIBUTOS': 'Tributos'}
SPANISH_ACCENTS = {'mexico': 'México', 'panama': 'Panamá', 'peru': 'Perú'}


def archival_fonds(archive, shelf, dt):
    """Fonds / series (container_title) from the reference code. -> (fonds, sure)"""
    a, s = norm(archive), clean(shelf)
    head = re.split(r'\s*[,:]\s*', s)[0].strip() if s else ''
    h = norm(head).replace('_', ' ')
    if 'archivo general de indias' in a and head:
        if h.startswith('mp-'):
            reg = h[3:].replace('_', ' ')
            reg = ', '.join(SPANISH_ACCENTS.get(x.strip(), x.strip().title()) for x in reg.split(' ')) \
                if reg not in ('santo domingo', 'buenos aires', 'peru chile', 'florida luisiana') else \
                {'santo domingo': 'Santo Domingo', 'buenos aires': 'Buenos Aires',
                 'peru chile': 'Perú y Chile', 'florida luisiana': 'Florida y Luisiana'}[reg]
            return 'Mapas y Planos, ' + reg, True
        if h in AGI_SECTIONS:
            return AGI_SECTIONS[h], True
        if h in AGI_AUDIENCIAS:
            name = {'mexico': 'México', 'panama': 'Panamá', 'santa fe': 'Santa Fe',
                    'santo domingo': 'Santo Domingo', 'buenos aires': 'Buenos Aires'}.get(h, h.title())
            return 'Gobierno, Audiencia de ' + name, True
    if 'nacion (colombia)' in a and head:
        if re.match(r'co\.agn\.s?mp', h):
            return 'Sección Mapas y Planos', True
        if head in AGN_CO_FONDO:
            return 'Colonia, ' + AGN_CO_FONDO[head], True
        m = re.match(r'([A-Z_]+)-(.+)$', head)
        if m and m.group(1) in AGN_CO_FONDO and m.group(2) in AGN_CO_REGION:
            return 'Colonia, {} {}'.format(AGN_CO_FONDO[m.group(1)], AGN_CO_REGION[m.group(2)]), True
        if not re.search(r'\d', head):
            return head, False
    if 'nacion (mexico)' in a and re.fullmatch(r'\d+', head or '') and dt in (HIST_MAP,):
        return 'Mapas, Planos e Ilustraciones', True
    if 'simancas' in a and head == 'MPD':
        return 'Mapas, Planos y Dibujos', True
    if 'simancas' in a and head:
        return head, True
    if head.lower().startswith('fondo'):
        return head, True
    if 'cipriano' in a:
        return 'Fondo David Mejía Velilla, Archivo Santiago Arroyo y Valencia, Provincia de Popayán, Gobierno, Censos', True
    if 'carpetas sueltas' in h:
        return 'Carpetas sueltas', True
    if 'obadiah rich' in h:
        return 'Obadiah Rich Collection', True
    return '', True


def norm(s):
    return unicodedata.normalize('NFKD', s or '').encode('ascii', 'ignore').decode().lower()


# ------------------------------------------------------------- type map ------
def legacy_doc_type(r, year):
    cl, tp = r['citlist'].strip(), norm(r['tipo'].strip())
    has_arch = bool(clean(r['Archivo']))
    if tp in ('mapa',) or (cl == 'Mapas' and tp == ''):
        return MOD_MAP if year and int(year) > 1824 and not has_arch else HIST_MAP
    if tp == 'atlas':
        return MONO if year and int(year) > 1824 else HIST_MAP
    if tp in ('archivalia', 'manuscrito'):
        return ARCH_DOC
    if tp in ('monografia', 'serie'):
        return PRINT if cl in ('Impreso', 'Primaria') else MONO
    if tp == 'articulo':
        return ART
    if tp == 'tesis':
        return THES
    if tp == 'ponencia':
        return ONL_S if clean(r['URL']) else CHAP
    if tp == 'catalogo':
        return FINDING
    if tp == 'base de datos':
        return DBASE
    if tp in ('impreso', 'periodico'):
        return PRINT
    if tp in ('edicion', 'documento'):
        return CRIT
    if tp == 'online':
        return ONL_O
    if tp == 'geoservice':
        return GEOSERVICE
    if tp == 'comic':
        return MONO
    if tp in ('generico',):
        return ''
    if tp == 'dubleta':
        return 'DUP'
    if cl == 'Online' or (not tp and clean(r['URL'])):
        return ONL_O
    if cl == 'Literatura':
        return MONO
    if cl == 'Primaria':
        return PRINT
    return ''


# ------------------------------------------------------------------ main -----
def convert():
    types = load_types()
    req, opt = load_matrix()
    with open(SRC, encoding='utf-8-sig', newline='') as f:
        rows = list(csv.DictReader(f, delimiter=';'))
    by_id = {r['FuenteID'].strip().lower(): r for r in rows}
    out, fetch_jobs = [], []

    for r in rows:
        fid = r['FuenteID'].strip()
        key = fid.lower()
        o = {k: '' for k in TEMPLATE + EXTRA}
        added, notes, review = set(), [], []
        supplied_src = set()        # template fields copied straight from a legacy column

        def put(field, val, derived=True):
            val = clean(val) if field not in ('notes',) else val
            if not val:
                return
            o[field] = val
            (added if derived else supplied_src).add(field)

        o['fuenteID'] = r['ftid'].strip()
        o['FuenteID'] = fid
        o['listed'] = 'no' if r['citlist'].strip() == 'No' else 'yes'
        o['tiempo'] = clean(r['tiempo'])
        o['flag'] = r['flag'].strip()
        o['wikiincluded'] = r['wikiincluded'].strip()
        o['legacy_type'] = '{} / {}'.format(r['citlist'].strip(), r['tipo'].strip()).strip(' /')
        o['Cita_completa'] = clean(r['Cita_completa'])
        cita = first_line(r['Cita_completa'])          # later lines are Excel duplicates
        en = first_line(r['en_'])
        titulo = first_line(r['Titulo'])
        # Titulo holding a whole citation: 'Author, "Title". En: Journal 6 (1986)'
        m = re.match(r'^(?:([^"“]+?),\s*)?["“](.+?)["”]\.?,?\s*en:\s*(.+)$', titulo, re.I)
        if m:
            if m.group(1) and not clean(r['Apellido']):
                r = dict(r)
                r['Apellido'] = ''
                r['Nombre'] = m.group(1)       # resolved below via sf()
                r['_author_text'] = m.group(1)
            titulo = m.group(2)
            if not en:
                mt = re.search(r',?\s*en:\s*(.+)$', cita, re.I)
                en = mt.group(1) if mt else m.group(3)
            notes.append('Legacy title held the full citation')
        rest_title = clean(r['Titulo'].replace('_x000d_', '\n').split('\n', 1)[1]) \
            if '_x000d_' in r['Titulo'] else ''
        if rest_title:
            notes.append('Variant title: ' + rest_title)

        # -- year / original ------------------------------------------------
        year, date_text = parse_year(r['Año'])
        oyear, odate_text = parse_year(r['Año original'])
        if clean(r['Año']) and not year and date_text != 's.f.':
            review.append('legacy Año "{}" not a year'.format(clean(r['Año'])))

        # -- doc type --------------------------------------------------------
        dt = legacy_doc_type(r, year or oyear)
        ov = OVERRIDES.get(key, {})
        dup_target = None
        if dt == 'DUP' or re.match(r'dubleta de', titulo, re.I):
            m = re.search(r'fuente\s*id\s+(\S+)', titulo, re.I)
            dup_target = m.group(1).strip(' .') if m else ''
            o['duplicate_of'] = dup_target
            tgt = by_id.get(dup_target.lower())
            ty_r = tgt if tgt else r
            y2 = parse_year(ty_r['Año'])[0]
            dt = legacy_doc_type(ty_r, y2) if tgt else ''
            if dt == 'DUP' or not dt:
                dt = MONO if r['Lugar'] else (HIST_MAP if 'MP-' in r['Signatura'] else '')
            if not tgt:
                review.append('duplicate target "{}" not found'.format(dup_target))
            # real title lives in Cita_completa
            m = re.match(r"^(?:[^,']+,\s*)?'?([^'(]+?)'?\s*(?:\(|,\s*en:|$)", cita)
            titulo = strip_end(m.group(1)) if m and cita else ''
            if titulo:
                added.add('title')
        if ov.get('doc_type'):
            dt = ov['doc_type']

        # Literatura/Artículo that is really a book chapter: editors named, or a
        # "(Place Year)" imprint after a title that ends in no volume number
        if dt == ART and en and not ov.get('doc_type'):
            tail = en
            mt = re.search(r',?\s*en:\s*(.+)$', cita, re.I)
            if mt and mt.group(1)[:12].lower().strip() == en[:12].lower().strip():
                tail = mt.group(1)
            mi = re.search(r'\(([A-ZÁÉÍÓÚ][^()\d]+?),?\s+\d{4}\)', tail)
            imprint = bool(mi) and not re.search(r'(\d|\b[IVXLC]+)$', tail[:mi.start()].rstrip())
            journalish = bool(JOURNAL_WORDS.search(tail)) or bool(
                re.search(r'\b(?:vol\.|n[úu]m\.|no\.)', tail, re.I))
            if EDITOR_RX.search(tail):
                dt = CHAP
            elif imprint and not journalish:
                dt = CHAP
                review.append('Artículo reclassified as Chapter (container has a book imprint)')
        # Critical edition that is really an original print (no later edition)
        if dt == CRIT and not oyear and not en and year and int(year) <= 1830 and not ov.get('doc_type'):
            dt = PRINT
        # monograph published before 1830 by a contemporary -> print
        if dt == MONO and year and int(year) <= 1824 and not ov.get('doc_type'):
            dt = PRINT
            review.append('Monografía printed {} reclassified as Print (primary)'.format(year))

        o['doc_type'] = dt
        if dt in types:
            o['class'], o['ris_type'] = types[dt]
        elif dt:
            review.append('doc_type not in Types sheet')
        if r['tipo'].strip() == 'genérico':
            review.append('placeholder entry (no real source); no doc_type assigned')
            o['class'] = ''

        # -- authors -----------------------------------------------------------
        nombre, apellido = clean(r['Nombre']), clean(r['Apellido'])
        if apellido in ('NN.', 'NN', 'N.N.'):
            apellido = ''
        author_val = ''
        if r.get('_author_text'):
            author_val, ok = people_sf(r['_author_text'])
            added.add('author')
            if not ok:
                review.append('author split out of the legacy title — check')
            nombre = ''
        elif apellido:
            toks = nombre.split()
            if toks and apellido.split()[0] == toks[-1]:          # Juan del Pino / Pino Manrique
                nombre = ' '.join(toks[:-1])
                if nombre.split() and nombre.split()[-1].lower() in PARTICLES:
                    pass
            author_val = '{}, {}'.format(apellido, nombre) if nombre else apellido
        elif nombre:
            author_val = nombre
            review.append('only a forename in legacy data')
        extra = clean(r['Autores_adicionales'])
        editors = ''
        if extra:
            role_ed = bool(ROLE_RX.search(extra))
            ppl, sure = people_sf(extra)
            if role_ed:
                editors = '; '.join(x for x in [author_val, ppl] if x)
                author_val = ''
                added.add('editor')
            else:
                author_val = '; '.join(x for x in [author_val, ppl] if x)
                added.add('author')
            if not sure:
                review.append('additional names split into surname/forename by rule — check')
        if author_val and ROLE_RX.search(author_val):          # "Robledo (ed.), Emilio"
            ed = ROLE_RX.sub(' ', author_val).replace(' ,', ',')
            editors = '; '.join(x for x in [editors, clean(ed)] if x)
            author_val = ''
            added.add('editor')
        if author_val:
            o['author'] = author_val
            if 'author' not in added:
                supplied_src.add('author')
        if editors:
            o['editor'] = editors

        # author written into Titulo ("Manfred Kossok, El virreinato …")
        if not o['author'] and titulo and not ov.get('author'):
            m = re.match(r'^([A-ZÁÉÍÓÚ][a-záéíóúñ]+(?: (?:de |del |de la |y )?[A-ZÁÉÍÓÚ][a-záéíóúñ]+){1,3}), (.{10,})$',
                         titulo)
            if m and cita.startswith(m.group(1)):
                v, ok = sf(m.group(1))
                put('author', v)
                titulo = m.group(2)
                if not ok:
                    review.append('author split out of title — check')

        # -- title -------------------------------------------------------------
        title_from_url = False
        tl = titulo
        mt = re.search(r'\s*\(([^()]*?)\s*(\d{4})\)\.?$', tl)       # "Title (Place 1959)."
        if mt and dt in (MONO, PRINT, CRIT, THES, FINDING):
            if not r['Lugar'].strip() and mt.group(1):
                put('place', mt.group(1))
            if not year:
                year = mt.group(2)
                added.add('year')
            tl = tl[:mt.start()]
        tl = tl.strip().strip('"“”«»').strip()
        if tl.startswith("'") and tl.endswith("'") and tl.count("'") == 2:
            tl = tl[1:-1]
        tl = strip_end(tl) if not tl.endswith('...') else tl
        if is_url(tl):
            title_from_url = True
            tl = ''
        if tl:
            o['title'] = tl
            if 'title' not in added:
                supplied_src.add('title')
        else:
            # try Cita_completa: "Author, 'Title', en: …" or "Author, Title (Place Year)"
            m = re.search(r"['\"“]([^'\"”]{4,})['\"”]", cita)
            cand = m.group(1) if m else ''
            if not cand and cita and not is_url(cita):
                c2 = re.sub(r'\s*\([^()]*\d{4}\)\s*$', '', cita)
                sur = norm(o['author'].split(',')[0].split()[-1]) if o['author'] else ''
                if sur and ', ' in c2 and sur in norm(c2.split(', ', 1)[0]):
                    c2 = c2.split(', ', 1)[1]
                cand = c2
            if cand and not is_url(cand):
                put('title', strip_end(cand))
            else:
                title_from_url = True

        # -- simple copies ---------------------------------------------------
        if r['Lugar'].strip() and not o['place']:
            lug = clean(r['Lugar'])
            if lug.lower() == 'born digital':
                notes.append('Born digital')
            else:
                o['place'] = lug
                supplied_src.add('place')
        if year:
            o['year'] = year
            if 'year' not in added:
                supplied_src.add('year')
        if date_text:
            o['date_text'] = date_text
            added.add('date_text')
        url = fix_url(r['URL'])
        if clean(r['URL']) and not url:
            review.append('legacy URL "{}" is not a URL — dropped'.format(clean(r['URL'])))
        if url:
            o['url'] = url
            (added if url != clean(r['URL']) else supplied_src).add('url')
        elif dt in (ONL_S, ONL_O, DBASE):
            m = re.search(r'(https?://\S+|www\.\S+)', cita)
            if m:
                put('url', fix_url(m.group(1).rstrip(".,'")))
        vols = clean(r['Vols'])
        if vols:
            o['num_volumes'] = vols
            supplied_src.add('num_volumes')
        arch = clean(r['Archivo'])
        if arch:
            city, nm = ARCHIVES.get(arch.lower().rstrip(','), ('', arch.rstrip(',')))
            o['archive'] = nm
            (added if nm != arch else supplied_src).add('archive')
            if city and dt in (ARCH_DOC, HIST_MAP, PRINT, 'Archival series/holding') and not o['place']:
                put('place', city)
            elif not city:
                review.append('archive city unknown')
        sig = strip_end(clean(r['Signatura']))
        if sig:
            o['shelfmark'] = sig
            (added if sig != clean(r['Signatura']) else supplied_src).add('shelfmark')

        # original date
        if oyear:
            if dt == CRIT:
                put('original_pub', odate_text or oyear)
            elif dt in (ARCH_DOC, HIST_MAP, PRINT):
                if not o['year']:
                    put('year', oyear)
                    if odate_text:
                        put('date_text', odate_text)
                elif oyear != o['year']:
                    notes.append('Original date: ' + (odate_text or oyear))
            elif dt in (CHAP, ART):
                notes.append('Edits/discusses a document of ' + (odate_text or oyear))
            else:
                notes.append('Original date: ' + (odate_text or oyear))

        # -- container (en_) -------------------------------------------------
        known = None
        for rx, vals, kadded, knote in KNOWN_CONTAINERS:
            if en and re.search(rx, en, re.I):
                known = (vals, kadded, knote)
                break
        if known:
            vals, kadded, knote = known
            for k, v in vals.items():
                if k == '_author':
                    if not o['author']:
                        put('author', v)
                    continue
                if k == '_container_author':
                    notes.append('Container author: ' + v)
                    continue
                if k == 'year':
                    if dt in (HIST_MAP, ARCH_DOC, PRINT):
                        continue                        # map/doc keeps own date
                    if o['year'] and o['year'] != v and dt == CRIT:
                        if not o['original_pub']:
                            put('original_pub', o['year'])
                    o['year'] = v
                    added.add('year')
                    continue
                if k == 'place' and dt in (HIST_MAP, ARCH_DOC, PRINT):
                    continue
                if k == 'publisher' and dt in (HIST_MAP, ARCH_DOC):
                    continue
                if k == 'editor' and dt in (HIST_MAP,):
                    continue
                if not o.get(k) or k in ('container_title',):
                    put(k, v)
            pj = parse_journal_ref(en)
            vm = re.search(r'tomo\s+([IVXLC]+|\d+)', en, re.I)
            if vm and dt in (CRIT, PRINT):
                put('volume', vm.group(1))
            im = re.search(r'no\.?\s*(\d+(?:\s*[-–]\s*\d+)?)', en, re.I)
            if im and 'Mercurio' in en:
                put('issue', im.group(1).replace(' ', ''))
            if pj['page_start']:
                put('page_start', pj['page_start'])
                put('page_end', pj['page_end'])
            if knote:
                review.append(knote)
            notes.append('Legacy "en": ' + en)
        elif en and dt in (ART, CRIT, PRINT, ONL_S, ONL_O) and (
                JOURNAL_WORDS.search(en) or re.search(r'\d', en)) and not EDITOR_RX.search(en):
            # journal-like container; prefer the fuller tail of Cita_completa
            src = en
            m = re.search(r",?\s*en:\s*(.+)$", cita) or re.search(r'["”]\s*:\s*(.+)$', cita)
            if m and m.group(1)[:12].lower().strip() == en[:12].lower().strip():
                src = m.group(1)
            pj = parse_journal_ref(src)
            if pj['url'] and not o['url']:
                put('url', pj['url'])
            put('container_title', pj['container_title'])
            for k in ('volume', 'issue', 'page_start', 'page_end'):
                put(k, pj[k])
            if pj['year'] and not o['year']:
                put('year', pj['year'])
            if pj['place'] and not o['place'] and dt == ART:
                notes.append('Place given with journal: ' + pj['place'])
            if not pj['ok']:
                review.append('container string not fully parsed: "{}"'.format(src))
        elif en:
            # book-like container (chapter, edited document, reproduced map)
            src = en
            m = re.search(r",?\s*en:\s*(.+)$", cita)
            if m and m.group(1)[:12].lower().strip() == en[:12].lower().strip():
                src = m.group(1)
            pb = parse_book_ref(src)
            put('container_title', pb['title'])
            if pb['people']:
                ppl, sure = people_from_ref(pb['people'])
                if dt in (CHAP, CRIT) and pb['role'] == 'editor':
                    put('editor', ppl)
                elif dt == CRIT and pb['role'] == 'author':
                    # author of the volume == author of the text -> nothing; else editor
                    if norm(ppl.split(',')[0]) not in norm(o['author']):
                        put('editor', ppl)
                elif dt == CHAP:
                    put('editor', ppl)
                    review.append('names before container taken as editors')
                else:
                    notes.append('Container by: ' + ppl)
                if not sure:
                    review.append('container names split by rule — check')
            for k in ('volume', 'num_volumes', 'page_start', 'page_end'):
                if pb[k] and not o[k]:
                    put(k, pb[k])
            if pb['place'] and not o['place'] and dt not in (HIST_MAP, ARCH_DOC):
                put('place', pb['place'])
            if pb['year']:
                if dt in (HIST_MAP, ARCH_DOC):
                    notes.append('Reproduced in: ' + src)
                elif o['year'] and o['year'] != pb['year'] and dt == CRIT:
                    if not o['original_pub']:
                        put('original_pub', o['year'])
                    o['year'] = pb['year']
                    added.add('year')
                elif not o['year']:
                    put('year', pb['year'])
            elif dt in (HIST_MAP, ARCH_DOC):
                notes.append('Reproduced in: ' + src)
            if not pb['ok']:
                review.append('container string not fully parsed: "{}"'.format(src))

        # pages that only Cita_completa has (articles / chapters)
        if dt in (ART, CHAP) and not o['page_start']:
            m = re.search(r'pp?\.\s*(\d+)\s*[-–]\s*(\d+)', cita)
            if m:
                put('page_start', m.group(1))
                put('page_end', m.group(2))
            else:
                m = re.search(r'(\d+)\s*pp\b', cita)
                if m:
                    notes.append('Extent: {} pp.'.format(m.group(1)))
        if o['page_start'] and o['page_end'] and o['page_start'].isdigit() and o['page_end'].isdigit() \
                and int(o['page_end']) < int(o['page_start']):
            review.append('page range {}-{} inverted'.format(o['page_start'], o['page_end']))
        # year / place that only Cita_completa has
        if not o['year'] and dt not in (ONL_S, ONL_O, DBASE, ''):
            m = re.findall(r'\((?:[^()]*\s)?(\d{4})\)', cita)
            if m and dt == CRIT and not o['original_pub'] and not o['place'] \
                    and int(m[-1]) <= 1830 and not ov.get('year'):
                # "(1796)" with no imprint = date of the text, not of the edition
                put('original_pub', m[-1])
                review.append('edition year unknown; the only date given ({}) taken as original date'
                              .format(m[-1]))
            elif m:
                put('year', m[-1])
        if not o['year'] and dt in (ARCH_DOC, HIST_MAP):
            ys = sorted(set(re.findall(r'\b(1[4-8]\d\d)\b', o['title'])))
            if ys:                                      # "Padrones … 1779, 1780 y 1786"
                put('year', ys[0])
                if len(ys) > 1:
                    put('date_text', '{}–{}'.format(ys[0], ys[-1]) if len(ys) == 2
                        else ', '.join(ys))
        if dt in (MONO, CHAP, THES, CRIT, PRINT) and not o['place']:
            m = re.search(r'\(([A-ZÁÉÍÓÚ][^()\d]{2,40}?),?\s+\d{4}\)\s*$', cita)
            if m and 'Tesis' not in m.group(1):
                put('place', m.group(1))

        # thesis: university from "(Tesis, Universidad X 2011)"
        if dt == THES:
            m = re.search(r'\((Tesis[^,]*),\s*([^()]+?)\s+\d{4}\)', cita)
            if m and not o['publisher']:
                put('publisher', m.group(2))

        # volume inside a title: "…, vol. 1" / "…, tomo II" -> volume, title stripped
        if o['title'] and dt in (MONO, PRINT, CRIT, FINDING):
            m = re.search(r',\s*(?:vol\.?|tomo|t\.)\s*([IVXLC]+|\d+)\s*$', o['title'], re.I)
            if m:
                if not o['volume']:
                    put('volume', m.group(1))
                o['title'] = o['title'][:m.start()].rstrip(' ,')
                added.add('title')
            else:
                m = re.search(r',\s*(?:vol\.?|tomo)\s*([IVXLC]+|\d+)\s*:', o['title'], re.I)
                if m and not o['volume']:
                    put('volume', m.group(1))

        # archival fonds / series -> container_title (required for archival documents)
        if o['archive'] and dt in (ARCH_DOC, HIST_MAP) and not o['container_title']:
            fonds, sure = archival_fonds(o['archive'], o['shelfmark'], dt)
            if fonds:
                put('container_title', fonds)
                if not sure:
                    review.append('fonds "{}" taken from the shelfmark prefix as written'.format(fonds))
            if 'cipriano' in norm(o['archive']):
                o['shelfmark'] = '1.3.3.2'
                added.add('shelfmark')
                review.append('archive location (Universidad de La Sabana, Chía) from bibliographic knowledge — verify')

        if dt == CHAP and norm(r['tipo']) == 'ponencia':
            notes.append('Ponencia (conference paper)')
        if dt == ONL_S and norm(r['tipo']) == 'ponencia':
            notes.append('Ponencia (conference paper)')
            if en and not o['container_title']:
                put('container_title', en)
        if r['tipo'].strip() == 'Geoservice':
            notes.append('Geoservice / online gazetteer')

        # online type refinement
        if dt in (ONL_O,) and not ov.get('doc_type'):
            u = o['url']
            for rx, t in DOMAIN_TYPES:
                if u and re.search(rx, u, re.I):
                    dt = t
                    break
            else:
                if nombre and apellido:
                    dt = ONL_S
            if dt != ONL_O:
                o['doc_type'] = dt
                o['class'], o['ris_type'] = types[dt]

        # -- overrides -------------------------------------------------------
        for k, v in ov.items():
            if k.startswith('_') or k == 'doc_type':
                continue
            o[k] = v
            if v:
                added.add(k)
            else:
                added.discard(k)
                supplied_src.discard(k)
        if 'container_title' in ov:
            review[:] = [x for x in review if not x.startswith(('container string', 'names before',
                                                               'container names'))]
        if 'author' in ov:
            review[:] = [x for x in review if not x.startswith(('author split', 'additional names'))]
        if ov.get('_review'):
            review.append(ov['_review'])
        for k in ov.get('_added', []):
            added.add(k)

        # short_title: siglum for archival series only (required there)
        # language
        lang = guess_lang(o['title']) or guess_lang(o['title'] + ' ' + o['container_title'])
        if lang:
            o['language'] = lang
            added.add('language')

        # title for bare-URL online sources -> fetch later
        if title_from_url and not o['title']:
            u = o['url'] or fix_url(titulo) or fix_url(cita.strip("'"))
            if u and not o['url']:
                put('url', u)
            if u:
                fetch_jobs.append((len(out), u))
            else:
                review.append('no title and no URL')

        if not titulo and not cita and not o['url'] and not o['author']:
            review.append('empty legacy record — only the FuenteID is known')

        o['notes'] = '; '.join(dict.fromkeys(notes))
        if o['notes']:
            added.add('notes')
        o['_added'], o['_supplied_src'], o['_review'] = added, supplied_src, review
        out.append(o)

    # --- fetch page titles for bare-URL online sources ------------------------
    def is_home(u):
        return re.fullmatch(r'https?://[^/]+/?', u or '') is not None

    def known_site(u):
        return SITE_TITLES.get(site_name(u)) or SITE_TITLES.get(site_name(u).split('.', 1)[-1])

    todo = [j for j in fetch_jobs if not (is_home(j[1]) and known_site(j[1]))]
    if FETCH_TITLES and todo:
        with ThreadPoolExecutor(16) as ex:
            fetched = dict(zip([j[0] for j in todo], ex.map(lambda j: fetch_title(j[1]), todo)))
    else:
        fetched = {}
    for i, u in fetch_jobs:
        o = out[i]
        t = fetched.get(i, '')
        if is_home(u) and known_site(u):
            o['title'] = known_site(u)[0]
        elif t == '#REPURPOSED':
            o['title'] = site_name(u)
            o['_review'].append('URL now serves unrelated content (checked 2026-11-15); original title unknown')
        elif t and len(t) >= 5 and not re.fullmatch(r'(?i)territoriales|home|inicio|blog|index|bienvenidos?', t):
            o['title'] = t
            o['_review'].append('title taken from the web page <title> (fetched 2026-11-15)')
        elif o['doc_type'] not in (ONL_S, ONL_O, DBASE):
            o['_review'].append('no title; only a URL ({}) is recorded'.format(site_name(u)))
            continue
        else:
            o['title'] = site_name(u)
            o['_review'].append('title is only the site name (page not reachable or untitled)')
        o['_added'].add('title')
        if not o['language']:
            lang = guess_lang(o['title'])
            if lang:
                o['language'] = lang
                o['_added'].add('language')

    # --- bookkeeping columns --------------------------------------------------
    for o in out:
        dt = o['doc_type']
        if dt == ARCH_DOC and is_series(o):
            dt = o['doc_type'] = ARCH_SER
            o['class'], o['ris_type'] = types[dt]
            o['_review'].append('reclassified Archival document -> Archival series/holding '
                                '(collective title or whole legajo / legajo range) — check')
        if dt == MONO and o['editor'] and not o['author']:
            # a monograph credited only to editors/coordinators is an edited volume
            dt = o['doc_type'] = EDVOL
            o['class'], o['ris_type'] = types[dt]
        if dt == ART and o['place']:
            o['place'] = ''          # place of publication is not recorded for articles
            o['_added'].discard('place')
        ks = known_site(o['url']) if o['url'] else None
        if dt in (ONL_S, ONL_O) and not o['container_title'] and o['url']:
            # the website is the container
            o['container_title'] = ks[0] if ks else (
                'Wikipedia' if 'wikipedia.org' in o['url'] else site_name(o['url']))
            o['_added'].add('container_title')
        if dt in (ONL_S, ONL_O, DBASE) and ks and ks[1] and not o['publisher']:
            o['publisher'] = ks[1]
            o['_added'].add('publisher')
        filled = [f for f in TEMPLATE if f not in AUTO and o[f]]
        o['fields_supplied'] = ' '.join(filled)
        o['fields_added'] = ' '.join(f for f in filled if f in o['_added'])
        if dt in req:
            o['missing_required'] = ' '.join(f for f in TEMPLATE if f in req[dt]
                                             and f not in AUTO and not o[f])
            allowed = req[dt] | opt.get(dt, set())
            o['fields_outside_matrix'] = ' '.join(f for f in filled if f not in allowed)
        rv = list(dict.fromkeys(o['_review']))
        o['review'] = 'yes' if rv else ''
        o['review_note'] = ' | '.join(rv)
        for k in ('_added', '_supplied_src', '_review'):
            del o[k]

    with open(OUT, 'w', encoding='utf-8-sig', newline='') as f:
        w = csv.DictWriter(f, fieldnames=TEMPLATE + EXTRA, delimiter=';')
        w.writeheader()
        w.writerows(out)
    return out


if __name__ == '__main__':
    res = convert()
    from collections import Counter
    print(len(res), 'rows ->', OUT)
    print(Counter(o['doc_type'] for o in res).most_common())
    print('review:', sum(1 for o in res if o['review']))
