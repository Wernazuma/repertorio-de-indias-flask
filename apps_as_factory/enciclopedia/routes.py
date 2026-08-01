import csv, os, random, unicodedata, re
from flask import Flask, render_template, request, jsonify, Response, send_from_directory, abort, current_app
from rapidfuzz import fuzz as _rf_fuzz
from fuzzywuzzy import fuzz
from io import StringIO
from i18n import resolve_locale
from . import bp

# Configure Flask
#app = Flask(__name__, static_url_path='/static')

# Function to read data from CSV files
def read_csv(filename):
    data = []
    with open(filename, 'r', encoding='utf-8') as file:
        reader = csv.DictReader(file, delimiter=';')
        for row in reader:
            data.append(row)
    return data

# Read the CSV files
gz_entidades_csv = read_csv('data/gz_entidades.csv')
church_csv = read_csv('data/gz_iglesia.csv')
name_csv = read_csv('data/gz_nombres.csv')
category_csv = read_csv('data/gz_categoria.csv')
geometry_csv = read_csv('data/gz_geometry.csv')
cabildo_csv = read_csv('data/gz_cabildo.csv')
foreignkeys_csv = read_csv('data/gz_foreignkeys.csv')
reference_gazetteer_csv = read_csv('data/reference_gazetteer.csv')
contains_csv = read_csv('data/contains.csv')
gz_info_csv = read_csv('data/gz_info_1.csv')
capital_csv = read_csv('data/Cabeceras-Entidades.csv')
institution_csv = read_csv('data/instituciones.csv')

oficiales_entity_csv = read_csv('data/da_adm_Oficial_link.csv')
infotable_csv = read_csv('data/infotable.csv')
fuentes_entidades_csv = read_csv('data/entidades_fuentes.csv')
entidades_csv = read_csv('data/entidades.csv')
jerarquia_csv = read_csv('data/jerarquia.csv')

fuentes_csv = read_csv('data/fuentes.csv')
oficiales_csv = read_csv('data/da_adm_Oficiales.csv')
oficiales_foreignkeys_csv = read_csv('data/da_adm_Oficiales_foreignkeys.csv')


#############################################
### reference_gazetteer belonging helpers ###
#############################################
# reference_gazetteer.csv is wide: each place row carries one column-set per
# administrative level (jurisdiccion, provincia, ... obispado) rather than one
# row per belonging (as the old espartede.csv did). These helpers explode a wide
# row back into the per-level "belonging" dicts the templates/filters expect,
# keyed the same way espartede rows were (Nivel, polygon_*, overlap_*).

def _to_int(value, default):
    try:
        return int(float(value))
    except (ValueError, TypeError):
        return default

# The wide file has no per-level "type" column, and an entity's type is
# period-specific (e.g. JUNEMXMX was a Corregimiento 1574-1786, then a
# Jurisdiccion). We recover the type valid for a belonging's time slice from
# infotable.csv, falling back to the canonical entidades.csv type.
tipo_by_entidad = {e['Entidad']: e.get('tipo', '') for e in entidades_csv}
infotable_by_entidad = {}
for _inf in infotable_csv:
    infotable_by_entidad.setdefault(_inf['Entidad'], []).append(
        (_to_int(_inf.get('START'), -99999), _to_int(_inf.get('END'), 99999), _inf.get('tipo', ''))
    )

def _tipo_for_period(entidad, overlap_start):
    """Entity type valid at overlap_start (period-accurate), else canonical."""
    start = _to_int(overlap_start, None)
    if start is not None:
        for ist, ien, tp in infotable_by_entidad.get(entidad, []):
            if tp and ist <= start <= ien:
                return tp
    return tipo_by_entidad.get(entidad, '')

# (Nivel value used by templates, column prefix in reference_gazetteer.csv)
REF_LEVELS = [
    ('Jurisdiccion',    'jurisdiccion'),
    ('Provincia',       'provincia'),
    ('Provincia_Menor', 'provincia_menor'),
    ('Provincia_Mayor', 'provincia_mayor'),
    ('Partido',         'partido'),
    ('Audiencia',       'audiencia'),
    ('Obispado',        'obispado'),
    ('Principal',       'principal'),
]

# Structural levels offered as checkboxes in place search (coarse -> fine).
# Values match the `Nivel` produced by reference_belongings().
TERR_LEVEL_OPTIONS = ['Principal', 'Audiencia', 'Obispado', 'Provincia_Mayor',
                      'Provincia', 'Provincia_Menor', 'Partido', 'Jurisdiccion']

# Territory Entidad ID -> its generic region (kept for other callers).
entidad_region = {t['Entidad']: (t.get('Region') or '').strip()
                  for t in entidades_csv if t.get('Entidad')}

# Places carry a 3-letter region CODE (gz_entidades.region); territories carry a
# full name (entidades.Region). The place-search "generic region" filter narrows
# by the PLACE's own region, so it uses the codes (and the friendly names below).
REGION_CODE_TO_NAME = {
    "NES": "Nueva España",
    "GDJ": "Nueva Galicia y Septentrion",
    "GUA": "Guatemala",
    "SDO": "Santo Domingo",
    "VEN": "Venezuela",
    "TFI": "Tierra Firme",
    "NGR": "Nuevo Reino de Granada",
    "QUI": "Quito",
    "PER": "Peru",
    "CHA": "Charcas",
    "CHL": "Chile",
    "RPL": "Rio de la Plata",
    "FIL": "Filipinas",
    "EXT": "Exterior",
}

# gz_id -> place region code, and the region checkbox options (code, label) that
# actually occur among places, ordered by friendly name.
gz_region = {e['gz_id']: (e.get('region') or '').strip() for e in gz_entidades_csv}
PLACE_REGION_OPTIONS = sorted(
    {code for code in gz_region.values() if code},
    key=lambda c: REGION_CODE_TO_NAME.get(c, c),
)
PLACE_REGION_OPTIONS = [(c, REGION_CODE_TO_NAME.get(c, c)) for c in PLACE_REGION_OPTIONS]

# The reference gazetteer stops at a final time-slice (overlap_end caps ~1807).
# Concrete years beyond that would otherwise match no belonging slice, so the
# belonging year filter clamps the query at this ceiling. Set on index build.
_MAX_OVERLAP_END = 9999


# Lazy index for the belonging filter, so a name search compares against the
# ~1.7k DISTINCT territories, not ~650k exploded belonging rows.
#   name_index: entidad_id -> {'names': set(normalized), 'niveles': set, 'region': str}
#   members:    entidad_id -> [(gz_id, overlap_start, overlap_end), ...]
# Built on first use (needs normalize_string, which is defined further down).
_TERR_INDEX = None


def _build_territory_index():
    global _MAX_OVERLAP_END
    name_index, members = {}, {}
    max_end = 0
    for row in reference_gazetteer_csv:
        gz = row['gz_id']
        ostart = row.get('overlap_start', '')
        oend = row.get('overlap_end', '')
        _e = _to_int(oend, 0)
        if _e and _e < 9999 and _e > max_end:
            max_end = _e
        for nivel, prefix in REF_LEVELS:
            ent = (row.get(prefix + '_entidad_id') or '').strip()
            if not ent:
                continue
            rec = name_index.get(ent)
            if rec is None:
                names = set()
                for key in (prefix + '_nombre', prefix + '_label'):
                    v = (row.get(key) or '').strip()
                    if v:
                        names.add(normalize_string(v).lower())
                for vv in (row.get(prefix + '_variantes') or '').split('@'):
                    vv = vv.strip()
                    if vv:
                        names.add(normalize_string(vv).lower())
                name_index[ent] = {'names': names, 'niveles': {nivel},
                                   'region': entidad_region.get(ent, '')}
            else:
                rec['niveles'].add(nivel)
            members.setdefault(ent, []).append((gz, ostart, oend))
    if max_end:
        _MAX_OVERLAP_END = max_end
    return name_index, members


def _territory_index():
    global _TERR_INDEX
    if _TERR_INDEX is None:
        _TERR_INDEX = _build_territory_index()
    return _TERR_INDEX


def _territories_matching(qnorm, level_set):
    """Entidad IDs whose name fuzzy-matches qnorm (substring, then rapidfuzz
    token_set_ratio >= 88) and sit at one of the chosen structural level(s).
    (Generic region is a property of the PLACE, filtered separately.)"""
    name_index, _ = _territory_index()
    out = set()
    for ent, rec in name_index.items():
        if level_set and not (rec['niveles'] & level_set):
            continue
        if qnorm:
            hit = False
            for nm in rec['names']:
                if nm and (qnorm in nm or _rf_fuzz.token_set_ratio(qnorm, nm) >= 88):
                    hit = True
                    break
            if not hit:
                continue
        out.add(ent)
    return out


def _places_for_territories(match_ents, keep_ids, year_start, year_end):
    """Union the gz_ids that belong to any matched territory, restricted to
    keep_ids and (optionally) the year range via the belonging time slices."""
    _, members = _territory_index()
    # The data's final slice ends at _MAX_OVERLAP_END (~1807); clamp the query so
    # a concrete year at/after that boundary still matches the latest slice
    # instead of silently returning nothing.
    ys = min(year_start, _MAX_OVERLAP_END) if year_start is not None else None
    filtered = set()
    for ent in match_ents:
        for gz, os_, oe_ in members.get(ent, []):
            if gz not in keep_ids or gz in filtered:
                continue
            if ys is not None or year_end is not None:
                try:
                    s = int(float(os_)) if os_ else 0
                    e = int(float(oe_)) if oe_ else 9999
                    if ys is not None and e < ys:
                        continue
                    if year_end is not None and s > year_end:
                        continue
                except (ValueError, TypeError):
                    continue
            filtered.add(gz)
    return filtered


def reference_belongings(row):
    """Explode one wide reference_gazetteer row into per-level belonging dicts."""
    out = []
    ostart = row.get('overlap_start', '')
    oend = row.get('overlap_end', '')
    for nivel, prefix in REF_LEVELS:
        ent = (row.get(prefix + '_entidad_id') or '').strip()
        label = (row.get(prefix + '_label') or '').strip()
        if not ent and not label:
            continue
        out.append({
            'gz_id': row['gz_id'],
            'Nivel': nivel,
            'polygon_label': label,
            'polygon_nombre': row.get(prefix + '_nombre', ''),
            'polygon_variantes': row.get(prefix + '_variantes', ''),
            'polygon_entidad_id': ent,
            'polygon_tipo': _tipo_for_period(ent, ostart) or nivel,
            'overlap_start': ostart,
            'overlap_end': oend,
        })
    return out

# gz_id -> its wide rows (one per time slice), for quick per-place lookup.
reference_by_gzid = {}
for _r in reference_gazetteer_csv:
    reference_by_gzid.setdefault(_r['gz_id'], []).append(_r)

def reference_parts_for(gz_ids):
    """Yield exploded belonging dicts for the given gz_ids (used by search/download)."""
    for row in reference_gazetteer_csv:
        if row['gz_id'] not in gz_ids:
            continue
        for part in reference_belongings(row):
            yield part


#################################################
### Territory hierarchy (depends-on / subordinate)
#################################################
# The old jerarquia.csv only carried a partial hierarchy (often just the
# provincia link) and rendered the wrong time span. Instead we DERIVE the full
# stack from reference_gazetteer's per-level columns: for a territory E we look
# at the places it contains, read the higher-level slots (jurisdiccion,
# provincia*, audiencia, obispado, principal), and -- for obispados -- climb to
# the arzobispado via Obispado-Arzobispado.csv. Overlap ranges are merged, then
# split by the superior's period-specific type/name from infotable so, e.g.,
# PRGDDU00 shows "Gob.-Cap. Gen. de Nueva Vizcaya" until 1786 and
# "Gob.-Intendencia de Durango" thereafter.

obispado_arzobispado_csv = read_csv('data/Obispado-Arzobispado.csv')
arzobispado_by_obispado = {}
for _oa in obispado_arzobispado_csv:
    arzobispado_by_obispado.setdefault((_oa.get('Obispado') or '').strip(), []).append(_oa)

entidades_by_id = {e['Entidad']: e for e in entidades_csv}

# Entidad -> [(start, end, tipo, label)] period-specific attributes for display.
infotable_periods = {}
for _inf in infotable_csv:
    infotable_periods.setdefault(_inf['Entidad'], []).append((
        _to_int(_inf.get('START'), -99999), _to_int(_inf.get('END'), 99999),
        _inf.get('tipo', ''), _inf.get('Label', '')))

# Civil containment ladder (smallest first) + the cross-cutting slots.
_LADDER = ['partido', 'jurisdiccion', 'provincia_menor', 'provincia', 'provincia_mayor']
_ALL_SLOTS = _LADDER + ['audiencia', 'obispado', 'principal']

# For an entity at a given level, which superior levels the "depends on" shows.
_SUPERIOR_RULES = {
    'partido':         ['jurisdiccion', 'provincia_menor', 'provincia', 'provincia_mayor', 'obispado', 'audiencia', 'principal'],
    'jurisdiccion':    ['provincia_menor', 'provincia', 'provincia_mayor', 'obispado', 'audiencia', 'principal'],
    'provincia_menor': ['provincia', 'provincia_mayor', 'audiencia', 'principal'],
    'provincia':       ['provincia_mayor', 'audiencia', 'principal'],
    'provincia_mayor': ['audiencia', 'principal'],
    'audiencia':       ['principal'],
    'obispado':        ['arzobispado'],
    'arzobispado':     [],
    'principal':       [],
}
# Display/sort order of the superior (and subordinate) levels.
_LEVEL_ORDER = ['jurisdiccion', 'provincia_menor', 'provincia', 'provincia_mayor',
                'obispado', 'audiencia', 'arzobispado', 'principal', 'partido']

# entidad_id -> reference rows it appears in (any slot), once each.
ref_rows_by_entity = {}
for _r in reference_gazetteer_csv:
    _seen = set()
    for _pref in _ALL_SLOTS:
        _eid = (_r.get(_pref + '_entidad_id') or '').strip()
        if _eid and _eid not in _seen:
            _seen.add(_eid)
            ref_rows_by_entity.setdefault(_eid, []).append(_r)


def _row_level(eid, row):
    """The finest slot the entity fills in this particular reference row."""
    for lvl in _ALL_SLOTS:
        if (row.get(lvl + '_entidad_id') or '').strip() == eid:
            return lvl
    return None


def _entity_level(eid):
    """The finest slot the entity ever fills (its 'own' level), for sorting."""
    rows = ref_rows_by_entity.get(eid, [])
    for lvl in _ALL_SLOTS:
        if any((r.get(lvl + '_entidad_id') or '').strip() == eid for r in rows):
            return lvl
    return None


def _merge_intervals(intervals):
    """Merge overlapping/adjacent (gap <= 1) [start, end] year ranges."""
    out = []
    for s, e in sorted(set(intervals)):
        if out and s <= out[-1][1] + 1:
            out[-1] = (out[-1][0], max(out[-1][1], e))
        else:
            out.append((s, e))
    return out


def _period_rows(eid, a, b):
    """Split [a, b] by the entity's infotable periods -> (start, end, tipo, label)."""
    out = []
    for ps, pe, tipo, label in sorted(infotable_periods.get(eid, [])):
        s, e = max(a, ps), min(b, pe)
        if s <= e:
            out.append((s, e, tipo, label))
    if not out:
        ent = entidades_by_id.get(eid, {})
        out = [(a, b, ent.get('tipo', ''), ent.get('Nombre', ''))]
    return out


def _dedup_periods(rows):
    """Drop identical (tipo, label) periods and merge their contiguous spans."""
    grouped = {}
    for s, e, tipo, label in rows:
        grouped.setdefault((tipo, label), []).append((s, e))
    out = []
    for (tipo, label), ivs in grouped.items():
        for s, e in _merge_intervals(ivs):
            out.append((s, e, tipo, label))
    return out


def _min_level(levels):
    """The most-specific level (earliest in the display order) among those given."""
    return min(levels, key=lambda l: _LEVEL_ORDER.index(l) if l in _LEVEL_ORDER else 99)


def territory_superiors(eid):
    """Territories this one depends on, per the per-nivel rule, with
    period-accurate names and merged chronology."""
    rows = ref_rows_by_entity.get(eid, [])
    acc = {}          # superior entidad_id -> {'ivs': [(start,end)], 'lvls': {slot}}
    is_obispado = False
    for r in rows:
        rl = _row_level(eid, r)
        if rl is None:
            continue
        if rl == 'obispado':
            is_obispado = True
        os_ = _to_int(r.get('overlap_start'), None)
        oe = _to_int(r.get('overlap_end'), None)
        if os_ is None or oe is None:
            continue
        for slvl in _SUPERIOR_RULES.get(rl, []):
            if slvl == 'arzobispado':
                continue
            seid = (r.get(slvl + '_entidad_id') or '').strip()
            if seid and seid != eid:
                d = acc.setdefault(seid, {'ivs': [], 'lvls': set()})
                d['ivs'].append((os_, oe))
                d['lvls'].add(slvl)
    if is_obispado:  # obispado -> arzobispado from the dedicated table
        for oa in arzobispado_by_obispado.get(eid, []):
            aeid = (oa.get('Arzobispado') or '').strip()
            s = _to_int(oa.get('START'), None)
            e = _to_int(oa.get('END_'), None)
            if aeid and aeid != eid and s is not None and e is not None:
                d = acc.setdefault(aeid, {'ivs': [], 'lvls': set()})
                d['ivs'].append((s, e))
                d['lvls'].add('arzobispado')

    out = []
    for seid, d in acc.items():
        lvl = _min_level(d['lvls'])
        for a, b in _merge_intervals(d['ivs']):
            for s, e, tipo, label in _dedup_periods(_period_rows(seid, a, b)):
                out.append({'Ent_sup': seid, 'sup_tipo': tipo, 'sup_Label': label,
                            'level': lvl, 'START': s, 'END': e})
    out.sort(key=lambda d: (_LEVEL_ORDER.index(d['level']), d['START']))
    return out


def territory_subordinates(eid):
    """Territories that depend on this one (inverse of the rule)."""
    rows = ref_rows_by_entity.get(eid, [])
    acc = {}
    for r in rows:
        rl = _row_level(eid, r)
        if rl is None:
            continue
        os_ = _to_int(r.get('overlap_start'), None)
        oe = _to_int(r.get('overlap_end'), None)
        if os_ is None or oe is None:
            continue
        for slvl in _ALL_SLOTS:
            if slvl == rl or rl not in _SUPERIOR_RULES.get(slvl, []):
                continue
            feid = (r.get(slvl + '_entidad_id') or '').strip()
            if feid and feid != eid:
                d = acc.setdefault(feid, {'ivs': [], 'lvls': set()})
                d['ivs'].append((os_, oe))
                d['lvls'].add(slvl)
    # if E is an arzobispado, its subordinate obispados come from the table
    for oa in obispado_arzobispado_csv:
        if (oa.get('Arzobispado') or '').strip() == eid:
            feid = (oa.get('Obispado') or '').strip()
            s = _to_int(oa.get('START'), None)
            e = _to_int(oa.get('END_'), None)
            if feid and feid != eid and s is not None and e is not None:
                d = acc.setdefault(feid, {'ivs': [], 'lvls': set()})
                d['ivs'].append((s, e))
                d['lvls'].add('obispado')

    out = []
    for feid, d in acc.items():
        lvl = _min_level(d['lvls'])
        for a, b in _merge_intervals(d['ivs']):
            for s, e, tipo, label in _dedup_periods(_period_rows(feid, a, b)):
                out.append({'Entidad': feid, 'tipo': tipo, 'Label': label,
                            'level': lvl, 'START': s, 'END': e})
    out.sort(key=lambda d: (_LEVEL_ORDER.index(d['level']), d['START']))
    return out


########################
### Helper functions ###
########################

def stopwordremove(inputstring):
    # Remove specified Spanish stopwords
    stopwords = [' de ', ' la ', ' el ', ' del ', ' las ', ' los ', ' Nuestra Señora ']
    for stopword in stopwords:
        inputstring = inputstring.replace(stopword, ' ')
    inputstring = re.sub(r' +', ' ', inputstring)  # Remove extra spaces
    return inputstring.strip()

def normalize(inputstring):
    # Clean punctuation and apply transformations
    inputstring = re.sub(r'[\.,;:-]', '', str(inputstring))  # Clean punctuation
    inputstring=re.sub(r'á', 'a',  str(inputstring))
    inputstring=re.sub(r'é', 'e',  str(inputstring))
    inputstring=re.sub(r'í', 'i',  str(inputstring))
    inputstring=re.sub(r'ó', 'o',  str(inputstring))
    inputstring=re.sub(r'ú', 'u',  str(inputstring))
    inputstring = re.sub(r'x([aeiouáéíóú])', r'j\1', inputstring)
    inputstring = re.sub(r'X([aeiouáéíóú])', r'J\1', inputstring)
    inputstring = re.sub(r'g([éeií])', r'j\1', inputstring)
    inputstring = re.sub(r'G([éeíi])', r'J\1', inputstring)
    inputstring = re.sub(r'gu', 'hu', inputstring)
    inputstring = re.sub(r'Gu', 'Hu', inputstring)
    inputstring = re.sub(r'v', 'b', inputstring)
    inputstring = re.sub(r'tz', 'z', inputstring)
    inputstring = re.sub(r'y', 'i', inputstring)
    inputstring = re.sub(r'z', 's', inputstring)
    inputstring = re.sub(r's([éeíi])', r'c\1', inputstring)
    inputstring = re.sub(r'ñ', r'n', inputstring)
    inputstring = re.sub(r'Ñ', r'N', inputstring)
    inputstring = re.sub(r'([A-ZÑa-záéíóúüñç-]+)tan\b', r'\1tlan', inputstring)
    inputstring = re.sub(r'([A-ZÑa-záéíóúüñç-]+)cinco\b', r'\1cingo', inputstring)
    inputstring = re.sub(r'([A-ZÑa-záéíóúüñç-]+)zinco\b', r'\1cingo', inputstring)
    inputstring = re.sub(r' +', ' ', inputstring)  # Remove extra spaces
    return inputstring.strip()

def normalize_string(inputstring):
    # Normalize input while keeping the original string unchanged for display
    cleaned_string = stopwordremove(inputstring)
    normalized_string = normalize(cleaned_string)
    return normalized_string

# Helper function to calculate best Levenshtein match
def get_best_levenshtein_match(search_term, target_fields):
    """
    Calculate the best Levenshtein match score for a search term against multiple target fields.
    Returns the highest score found.
    """
    if not search_term or not target_fields:
        return 0
    
    best_score = 0
    search_term_normalized = normalize_string(search_term).lower()
    
    for field in target_fields:
        if field:
            # Compare with both original and normalized versions
            field_normalized = normalize_string(field).lower()
            score1 = fuzz.ratio(search_term_normalized, field.lower())
            score2 = fuzz.ratio(search_term_normalized, field_normalized)
            best_score = max(best_score, score1, score2)
    
    return best_score / 100.0  # Convert to 0-1 scale

def convert_decimal_commas(value):
    if isinstance(value, float):
        return str(value).replace('.', ',')
    if isinstance(value, str):
        try:
            f = float(value)
            return str(f).replace('.', ',')
        except:
            return value
    return value


####################
###### ROUTES ######
####################

# Home route (replaces /discover)
def _last_office(oficial_id):
    """Readable last office of an official, e.g. 'Obispo de Córdoba', taken from
    the office-link table (latest by Ultima_Fecha_num) — the same 'Titulo de
    entidad_nombre' the person detail page shows. Empty string if unknown."""
    if not oficial_id:
        return ''
    links = [l for l in oficiales_entity_csv if l.get('OficialID') == oficial_id]
    if not links:
        return ''
    def _num(l):
        try:
            return int(float(l.get('Ultima_Fecha_num') or 0))
        except (ValueError, TypeError):
            return 0
    last = max(links, key=_num)
    titulo = (last.get('Titulo') or '').strip()
    ent = (last.get('entidad_nombre') or last.get('Entidad_nombre_generico') or '').strip()
    office = '{} de {}'.format(titulo, ent) if titulo and ent else (titulo or ent)
    return office[:1].upper() + office[1:] if office else ''


def _random_previews():
    """One random place / territory / person / term for the home carousel."""
    items = []
    if gz_entidades_csv:
        p = random.choice(gz_entidades_csv)
        items.append({
            'kind_key': 'enc_card_places',
            'title': p.get('label') or p.get('nombre') or p.get('gz_id'),
            'subtitle': ', '.join(x for x in [p.get('partido_generico'),
                                              p.get('provincia_generica'), p.get('region')] if x),
            'url': f"/apps/enciclopedia/place/{p.get('gz_id')}",
        })
    if entidades_csv:
        tt = random.choice(entidades_csv)
        items.append({
            'kind_key': 'enc_card_territories',
            'title': tt.get('Nombre') or tt.get('Entidad'),
            'subtitle': ', '.join(x for x in [tt.get('tipo'), tt.get('Region')] if x),
            'url': f"/apps/enciclopedia/territory/{tt.get('Entidad')}",
        })
    if oficiales_csv:
        o = random.choice(oficiales_csv)
        birth = (o.get('Nacimiento_y') or '').strip()
        death = (o.get('Fallecimiento_y') or '').strip()
        if birth or death:
            meta = '({}–{})'.format(birth, death)  # (1650–1720), (1650–), (–1720)
        else:
            meta = ''
        items.append({
            'kind_key': 'enc_card_people',
            'title': o.get('NOM_entero') or o.get('OficialID'),
            'meta': meta,
            # Readable last office ("Obispo de Córdoba"), not the cargo_ultimo code.
            'subtitle': _last_office(o.get('OficialID')),
            'url': f"/apps/enciclopedia/people/persIndias{o.get('OficialID')}",
        })
    tdir = os.path.join(current_app.root_path, 'data', 'tesauro')
    if os.path.isdir(tdir):
        files = [f for f in os.listdir(tdir) if f.lower().endswith('.html')]
        if files:
            slug = random.choice(files)[:-5]
            lemma = lemma_by_id.get(slug)
            if lemma:
                en = (lemma.get('term-en') or '').strip()
                es = (lemma.get('termino-es') or '').strip()
                title = (en if resolve_locale() == 'en' and en else es) or slug.replace('_', ' ')
            else:
                title = slug.replace('_', ' ')
            items.append({
                'kind_key': 'enc_card_thesaurus',
                'title': title,
                'subtitle': '',
                'url': f"/apps/enciclopedia/tesauro/{slug}",
            })
    return items


@bp.route('/')
def index():
    places_count = len(gz_entidades_csv)
    territories_count = len(entidades_csv)
    officials_count = len(oficiales_csv)

    return render_template(
        'home.html',
        places=places_count,
        territories=territories_count,
        officials=officials_count,
        carousel=_random_previews()
    )


##################################
### Detail/Landing page routes ###
##################################

# Route for viewing place details
@bp.route('/place/<place_id>')
def place_detail(place_id):
    place = None
    for p in gz_entidades_csv:
        if p['gz_id'] == place_id:
            place = p
            break
    
    if place is None:
        return render_template('404.html'), 404

    church_data = [c for c in church_csv if c['gz_id'] == place_id]
    name_data = [n for n in name_csv if n['gz_id'] == place_id]
    category_data = [ct for ct in category_csv if ct['gz_id'] == place_id]
    geometry_data = [g for g in geometry_csv if g['gz_id'] == place_id]
    cabildo_data = [cb for cb in cabildo_csv if cb['gz_id'] == place_id]
    foreignkeys_data = [f for f in foreignkeys_csv if f['gz_id'] == place_id]
    espartede_data = []
    for _row in reference_by_gzid.get(place_id, []):
        espartede_data.extend(reference_belongings(_row))
    dependent_data = [d for d in gz_info_csv if d['es_parte_de'] == place_id]
    capital_data = [cp for cp in capital_csv if cp['gz_id'] == place_id]
    institution_data = [inst for inst in institution_csv if inst['gz_id'] == place_id]

    foreignkeys_data = sorted(foreignkeys_data, key=lambda x: x['foreignkey'], reverse=True)
    name_data = sorted(name_data, key=lambda x: x['start'])
    geometry_data = sorted(geometry_data, key=lambda x: x['start'])
    category_data = sorted(category_data, key=lambda x: x['start'])
    cabildo_data = sorted(cabildo_data, key=lambda x: x['start'])
    espartede_data = sorted(espartede_data, key=lambda x: x['overlap_start'])
    dependent_data = sorted(dependent_data, key=lambda x: x['start'])
    capital_data = sorted(capital_data, key=lambda x: (x['Entidad_ID'], x['start']))
    institution_data = sorted(institution_data, key=lambda x: x['START_'])

    # Two independent history sources, shown together when both are present:
    #   history_exists        -> dedicated historiographic article
    #   reconstruction_exists -> notes on reconstruction decisions / particularities
    history_exists = os.path.isfile(os.path.join(
        current_app.root_path, 'data', 'historia_gz_entidades', f'gz_{place_id}.html'))
    reconstruction_exists = os.path.isfile(os.path.join(
        current_app.static_folder, 'articulos', f'gz_{place_id}.html'))

    coordinates = [{'lat': float(g['lat']), 'lng': float(g['lon'])} for g in geometry_data if 'lat' in g and 'lon' in g]

    return render_template('place_detail.html', place=place, church_data=church_data,
                           name_data=name_data, geometry_data=geometry_data,
                           category_data=category_data, cabildo_data=cabildo_data,
                           foreignkeys_data=foreignkeys_data, espartede_data=espartede_data,
                           dependent_data=dependent_data, capital_data=capital_data,
                           institution_data=institution_data,
                           history_exists=history_exists, reconstruction_exists=reconstruction_exists,
                           coordinates=coordinates)

# Route for viewing territory details
@bp.route('/territory/<territory_id>')
def territory_detail(territory_id):
    territory = None
    for t in entidades_csv:
        if t['Entidad'] == territory_id:
            territory = t
            break
    
    if territory is None:
        return render_template('404.html'), 404

    oficiales_entity_data = [o for o in oficiales_entity_csv if o['EntidadID'] == territory_id]
    infotable_data = [inf for inf in infotable_csv if inf['Entidad'] == territory_id]
    fuentes_entidades_data = [f for f in fuentes_entidades_csv if f['entidadID'] == territory_id]
    # Full hierarchy derived from reference_gazetteer (+ Obispado-Arzobispado),
    # already sorted by level then chronology.
    jerarquia_sup_data = territory_superiors(territory_id)
    jerarquia_sub_data = territory_subordinates(territory_id)
    capital_terr_data = [cp for cp in capital_csv if cp['Entidad_ID'] == territory_id]

    oficiales_entity_data = sorted(oficiales_entity_data, key=lambda x: x['Ultima_Fecha_num'])
    infotable_data = sorted(infotable_data, key=lambda x: x['START'])
    capital_terr_data = sorted(capital_terr_data, key=lambda x: x['start'])

    fuentes_entidades_grouped = {}
    for fuente in fuentes_entidades_data:
        grupo = fuente['grupo']
        if grupo not in fuentes_entidades_grouped:
            fuentes_entidades_grouped[grupo] = []
        fuentes_entidades_grouped[grupo].append(fuente)
    
    for grupo, fuentes in fuentes_entidades_grouped.items():
        fuentes_entidades_grouped[grupo] = sorted(
            fuentes,
            key=lambda x: (
                x['tiempo'],  # sort lexicographically by tiempo as a string
                'si' != x['foco'],  # custom order: 'si' comes first
                'no' != x['foco'],
                'implicito' != x['foco']
            )
        )


    # History article + HGIS-Indias comment each come from an HTML chunk per entidad.
    article_exists = os.path.isfile(os.path.join(
        current_app.root_path, 'data', 'historia_entidades', f'{territory_id}.html'))
    comment_exists = os.path.isfile(os.path.join(
        current_app.root_path, 'data', 'historia_entidades', 'comentarioshgis', f'{territory_id}.html'))

    return render_template('territory_detail.html', territory=territory,
                           oficiales_entity_data=oficiales_entity_data, infotable_data=infotable_data,
                           fuentes_entidades_data=fuentes_entidades_grouped, jerarquia_sup_data=jerarquia_sup_data,
                           jerarquia_sub_data=jerarquia_sub_data, capital_terr_data=capital_terr_data,
                           article_exists=article_exists, comment_exists=comment_exists)



# Route for viewing people details
@bp.route('/people/persIndias<OficialID>')
def people_detail(OficialID):
    people = None
    for o in oficiales_csv:
        if o.get('OficialID') == OficialID:
            people = o
            break
    
    if people is None:
        return render_template('404.html'), 404

    oficiales_entity_data = [o for o in oficiales_entity_csv if o.get('OficialID') == OficialID]
    oficiales_foreignkeys_data = [f for f in oficiales_foreignkeys_csv if f.get('OficialID') == OficialID]
    oficiales_entity_data = sorted(oficiales_entity_data, key=lambda x: x.get('Ultima_Fecha_num'))

    return render_template('people_detail.html', people=people,
                           oficiales_entity_data=oficiales_entity_data, oficiales_foreignkeys_data=oficiales_foreignkeys_data)


#####################
### Search routes ###
#####################

@bp.route('/people/search', methods=['GET', 'POST'])
def people_search():
    # Retrieve and process the last_name_query
    last_name_query = request.args.get('last_name', '').strip()
    year_query = request.args.get('year', type=int)
    fuzzy_search = request.args.get('fuzzy_search') == 'on'

    # Split the last_name_query on "OR" and normalize each term
    last_name_terms = [normalize_string(term.strip()) for term in last_name_query.split('OR')] if last_name_query else []

    def matches_last_name(person, terms, fuzzy=False):
        if not terms:
            return True
            
        # Get searchable field
        searchable_fields = [person.get('Apellidos', '')]
        
        if fuzzy:
            # Use Levenshtein matching with threshold 0.8
            threshold = 0.8
            for term in terms:
                score = get_best_levenshtein_match(term, searchable_fields)
                if score >= threshold:
                    return True
            return False
        else:
            # Word-based matching with wildcard support
            for term in terms:
                apellidos = person.get('Apellidos', '')
                if not apellidos:
                    continue
                
                # Handle wildcard matching
                if '*' in term:
                    # Convert wildcard to regex pattern
                    pattern = term.replace('*', '.*')
                    # Check both original and normalized versions
                    if re.search(r'\b' + pattern + r'\b', apellidos, re.IGNORECASE) or \
                       re.search(r'\b' + pattern + r'\b', normalize_string(apellidos), re.IGNORECASE):
                        return True
                else:
                    # Word boundary matching
                    if re.search(r'\b' + re.escape(term) + r'\b', apellidos, re.IGNORECASE) or \
                       re.search(r'\b' + re.escape(term) + r'\b', normalize_string(apellidos), re.IGNORECASE):
                        return True
            return False

    results = []
    for o in oficiales_csv:
        try:
            range_s = int(float(o.get('RANGE_S', 0)))
            range_e = int(float(o.get('RANGE_E', 0)))
        except ValueError:
            continue

        # Check for matches on any of the last name terms
        last_name_match = matches_last_name(o, last_name_terms, fuzzy_search)
        year_match = (range_s <= year_query < range_e) if year_query else True

        if last_name_match and year_match:
            results.append(o)

    # Sort results by last names for better user experience
    results = sorted(results, key=lambda x: x['Apellidos'].lower())

    return render_template('people_search.html', results=results, last_name=last_name_query, year=year_query, fuzzy_search=fuzzy_search)


@bp.route('/territory/search', methods=['GET'])
def territory_search():
    name_query = request.args.get('name', '').strip()
    region = request.args.get('region', '').strip()
    tipo = request.args.get('tipo', '').strip()
    entidad = request.args.get('Entidad', '').strip()
    year_start = request.args.get('year_start', type=int)
    year_end = request.args.get('year_end', type=int)
    year = request.args.get('year', type=int)
    fuzzy_search = request.args.get('fuzzy_search') == 'on'

    if year is not None:
        year_start = year_end = year

    # Split search terms by "OR" and normalize
    name_terms = [normalize_string(term.strip()) for term in name_query.split('OR')] if name_query else []

    regions = sorted({t['Region'] for t in entidades_csv if t['Region']})
    tipos = sorted({t['tipo'] for t in entidades_csv if t['tipo']})
    entidades = sorted({t['Entidad'] for t in entidades_csv if t['Entidad']})

    def matches_territory_name(territory, terms, fuzzy=False):
        if not terms:
            return True
            
        # Get all searchable fields
        searchable_fields = [
            territory['Nombre'],
            territory.get('Variantes', ''),
        ]
        
        if fuzzy:
            # Use Levenshtein matching with threshold 0.8
            threshold = 0.8
            for term in terms:
                score = get_best_levenshtein_match(term, searchable_fields)
                if score >= threshold:
                    return True
            return False
        else:
            # Word-based matching with wildcard support
            for term in terms:
                for field in searchable_fields:
                    if not field:
                        continue
                    
                    # Handle wildcard matching
                    if '*' in term:
                        # Convert wildcard to regex pattern
                        pattern = term.replace('*', '.*')
                        # Check both original and normalized versions
                        if re.search(r'\b' + pattern + r'\b', field, re.IGNORECASE) or \
                           re.search(r'\b' + pattern + r'\b', normalize_string(field), re.IGNORECASE):
                            return True
                    else:
                        # Word boundary matching
                        if re.search(r'\b' + re.escape(term) + r'\b', field, re.IGNORECASE) or \
                           re.search(r'\b' + re.escape(term) + r'\b', normalize_string(field), re.IGNORECASE):
                            return True
            return False

    # Initial match on name/region/tipo/Entidad from entidades_csv
    prefiltered = [
        t for t in entidades_csv if 
        matches_territory_name(t, name_terms, fuzzy_search) and
        (region == '' or t['Region'] == region) and
        (tipo == '' or t['tipo'] == tipo) and
        (entidad == '' or t['Entidad'] == entidad)
    ]

    # If year filtering is active, filter based on infotable_csv using START/END_
    if year_start is not None or year_end is not None:
        # default boundaries
        y_start = year_start if year_start is not None else -9999
        y_end = year_end if year_end is not None else 9999

        # Build a set of Entidad values that have a matching row in infotable_csv
        valid_entidades = set()
        for row in infotable_csv:
            try:
                row_start = int(float(row.get('START', -9999)))
                row_end = int(float(row.get('END_', 9999)))
                if row_end >= y_start and row_start <= y_end:
                    valid_entidades.add(row['Entidad'])
            except (ValueError, TypeError):
                continue

        # Filter only if Entidad is in the valid set
        results = [t for t in prefiltered if t['Entidad'] in valid_entidades]
    else:
        results = prefiltered

    results = sorted(results, key=lambda x: (x['Nombre'].lower(), x['Region'].lower()))

    return render_template(
        'territory_search.html',
        results=results,
        regions=regions,
        tipos=tipos,
        entidades=entidades,
        name_query=name_query,
        selected_region=region,
        selected_tipo=tipo,
        selected_entidad=entidad,
        year_start=year_start,
        year_end=year_end,
        year=year,
        fuzzy_search=fuzzy_search
    )

@bp.route('/place/search', methods=['GET', 'POST'])
def place_search():
    # Cache for normalized strings
    normalization_cache = {}
    
    def cached_normalize(text):
        if not text:
            return ''
        if text not in normalization_cache:
            normalization_cache[text] = normalize_string(text)
        return normalization_cache[text]

    # Retrieve all search parameters
    name_query = request.args.get('name', '').strip()
    # Belonging filter (from the reference gazetteer): keep places that belonged
    # to a territory whose Nombre/Variantes fuzzy-matches `terr_name`, optionally
    # narrowed to chosen structural level(s) and/or generic region(s).
    terr_name = request.args.get('terr_name', '').strip()
    terr_levels = [l.strip() for l in request.args.getlist('terr_level') if l.strip()]
    # Generic region narrows by the PLACE's own region code (not the territory's).
    place_regions = [r.strip() for r in request.args.getlist('region') if r.strip()]
    year_start = request.args.get('year_start', type=int)
    year_end = request.args.get('year_end', type=int)
    category_query = request.args.get('category', '').strip()
    subcategory_query = request.args.get('subcategory', '').strip()
    church_category = request.args.get('church_category', '').strip()
    religious_order = request.args.get('religious_order', '').strip()
    year = request.args.get('year', type=int)
    fuzzy_search = request.args.get('fuzzy_search') == 'on'

    if year is not None:
        year_start = year_end = year

    # Define church category mappings
    church_category_mappings = {
        'curatos': ['Curato', 'Parroquia', 'Vicaria', 'Sagrario'],
        'misiones': ['Mision capital', 'Mision cabecera', 'Mision'],
        'curatos_misiones': ['Curato', 'Parroquia', 'Vicaria', 'Sagrario', 
                            'Mision capital', 'Mision cabecera', 'Mision'],
        'sede_obispado': ['Sagrario']
    }

    # Get all religious orders for the dropdown
    religious_orders = sorted(list(set(
        info.get('servido_por') for info in gz_info_csv 
        if info.get('servido_por')
    )))

    # Get subcategories for current category if one is selected
    subcategories = []
    if category_query:
        subcategories = sorted(list(set(
            info['categoria_especial'] for info in gz_info_csv 
            if info.get('categoria') == category_query and info.get('categoria_especial')
        )))

    # Split search terms by "OR" and normalize
    name_terms = [cached_normalize(term.strip()) for term in name_query.split('OR')] if name_query else []

    # Step 1: Initial set of matching GZ IDs based on category, subcategory, church category, and religious order
    matching_gz_ids = set()
    for info in gz_info_csv:
        # Check main category
        if category_query and info.get('categoria') != category_query:
            continue
            
        # Check subcategory if specified
        if subcategory_query and info.get('categoria_especial') != subcategory_query:
            continue

        # Check church category if specified
        if church_category:
            church_types = church_category_mappings.get(church_category, [])
            if info.get('iglesia_cat') not in church_types:
                continue

        # Check religious order if specified
        if religious_order and info.get('servido_por') != religious_order:
            continue

        # Apply temporal filtering to gz_info_csv
        if year_start is not None or year_end is not None:
            try:
                start = int(float(info.get('start', '0'))) if info.get('start') else 0
                end = int(float(info.get('end_', '9999'))) if info.get('end_') else 9999
                if year_start is not None and end < year_start:
                    continue
                if year_end is not None and start > year_end:
                    continue
            except (ValueError, TypeError):
                continue

        matching_gz_ids.add(info['gz_id'])

    # Step 2: Filter by name and variants in gz_entidades
    if name_terms:
        def matches_name_terms(entity, terms, fuzzy=False):
            if not terms:
                return True
                
            # Get all searchable fields
            searchable_fields = [
                entity['nombre'],
                entity['label'],
                entity.get('variantes', ''),
            ]
            
            if fuzzy:
                # Use Levenshtein matching with threshold 0.8
                threshold = 0.8
                for term in terms:
                    score = get_best_levenshtein_match(term, searchable_fields)
                    if score >= threshold:
                        return True
                return False
            else:
                # Word-based matching with wildcard support
                for term in terms:
                    for field in searchable_fields:
                        if not field:
                            continue
                        
                        # Handle wildcard matching
                        if '*' in term:
                            # Convert wildcard to regex pattern
                            pattern = term.replace('*', '.*')
                            # Check both original and normalized versions
                            if re.search(r'\b' + pattern + r'\b', field, re.IGNORECASE) or \
                               re.search(r'\b' + pattern + r'\b', cached_normalize(field), re.IGNORECASE):
                                return True
                        else:
                            # Word boundary matching
                            if re.search(r'\b' + re.escape(term) + r'\b', field, re.IGNORECASE) or \
                               re.search(r'\b' + re.escape(term) + r'\b', cached_normalize(field), re.IGNORECASE):
                                return True
                return False

        filtered_gz_ids = set()
        for entity in gz_entidades_csv:
            if entity['gz_id'] not in matching_gz_ids:
                continue
            
            if matches_name_terms(entity, name_terms, fuzzy_search):
                filtered_gz_ids.add(entity['gz_id'])
        
        matching_gz_ids = filtered_gz_ids

    # Step 2b: Generic-region filter on the PLACE's own region code.
    if place_regions:
        rset = set(place_regions)
        matching_gz_ids = {gz for gz in matching_gz_ids if gz_region.get(gz) in rset}

    # Step 3: Belonging filter (reference gazetteer). Keep places that belonged to
    # a territory matching the given name (fuzzy on Nombre/Variantes), optionally
    # narrowed to chosen level(s), honouring the time slices.
    if terr_name or terr_levels:
        qnorm = normalize_string(terr_name).lower() if terr_name else ''
        match_ents = _territories_matching(qnorm, set(terr_levels))
        matching_gz_ids = _places_for_territories(match_ents, matching_gz_ids,
                                                  year_start, year_end)

    # Step 4: Retrieve final results
    results = [
        {
            'gz_id': entity['gz_id'],
            'label': entity['label'],
            'nombre': entity['nombre'],
            'partido_generico': entity.get('partido_generico', 'N/A'),
            'provincia_generica': entity.get('provincia_generica', 'N/A'),
            'region': entity.get('region', 'N/A')
        }
        for entity in gz_entidades_csv if entity['gz_id'] in matching_gz_ids
    ]

    # Sort results
    results = sorted(results, key=lambda x: x['gz_id'].lower())

    return render_template(
        'place_search.html',
        results=results,
        name=name_query,
        region_options=PLACE_REGION_OPTIONS,
        level_options=TERR_LEVEL_OPTIONS,
        terr_name=terr_name,
        terr_levels=terr_levels,
        place_regions=place_regions,
        year_start=year_start,
        year_end=year_end,
        category=category_query,
        subcategory=subcategory_query,
        subcategories=subcategories,
        church_category=church_category,
        religious_order=religious_order,
        religious_orders=religious_orders,
        fuzzy_search=fuzzy_search
    )



###########################
### Table detail routes ###
###########################
# Route for viewing source details
@bp.route('/fuentes/<source_id>')
def source_detail(source_id):
    source_id = source_id.strip().lower()
    source = None
    for s in fuentes_csv:
        csv_source_id = s['FuenteID'].strip().lower()
        if csv_source_id == source_id:
            source = s
            break
    
    if source is None:
        return render_template('404.html'), 404

    return render_template('source_detail.html', source=source)


@bp.route('/instituciones/<InstID>')
def institucion_detail(InstID):
    rows = [i for i in institution_csv if i['InstID'] == InstID]
    if not rows:
        return render_template('404.html'), 404

    # All rows of an InstID share Label/Tipo/Fuente; they differ by Cardinalidad
    # and/or settlement over time.
    base = rows[0]
    cardinalidad_data = sorted(rows, key=lambda x: _to_int(x.get('START_'), 0))

    # Settlements can change over time -> distinct (gz_id, Lugar) with merged spans.
    sett_groups = {}
    for r in rows:
        gz = (r.get('gz_id') or '').strip()
        lugar = (r.get('Lugar') or '').strip()
        if not gz and not lugar:
            continue
        s = _to_int(r.get('START_'), None)
        e = _to_int(r.get('END_'), None)
        sett_groups.setdefault((gz, lugar), []).append((s, e))
    settlement_data = []
    for (gz, lugar), ivs in sett_groups.items():
        numeric = [(s, e) for s, e in ivs if s is not None and e is not None]
        if numeric:
            for a, b in _merge_intervals(numeric):
                settlement_data.append({'gz_id': gz, 'Lugar': lugar, 'START_': a, 'END_': b})
        else:
            settlement_data.append({'gz_id': gz, 'Lugar': lugar, 'START_': None, 'END_': None})
    settlement_data.sort(key=lambda x: (x['START_'] if x['START_'] is not None else 0))

    # distinct sources for the Resources & Sources tab
    fuentes = []
    for r in rows:
        f = (r.get('Fuente') or '').strip()
        if f and f not in fuentes:
            fuentes.append(f)

    article_exists = os.path.isfile(os.path.join(
        current_app.root_path, 'data', 'historia_instituciones', f'{InstID}.html'))

    return render_template('institucion_detail.html', inst=base,
                           cardinalidad_data=cardinalidad_data,
                           settlement_data=settlement_data, fuentes=fuentes,
                           article_exists=article_exists)


# Controlled-vocabulary term relations (parent / child / related). The CSV is
# coming; load it gracefully so the term pages work (with empty relations) until
# it exists. Fields: ID, termino-es, term-en, parent, related.
try:
    tesauro_relaciones_csv = read_csv('data/tesauro_relaciones.csv')
except (FileNotFoundError, OSError):
    tesauro_relaciones_csv = []
tesauro_by_id = {t.get('ID', '').strip(): t for t in tesauro_relaciones_csv if t.get('ID')}

# Index of lemmata, built from the data/tesauro/*.html filenames (tesauro.csv:
# ID, termino-es, term-en). Drives the thesaurus index and the display names.
try:
    tesauro_lemmata_csv = read_csv('data/tesauro.csv')
except (FileNotFoundError, OSError):
    tesauro_lemmata_csv = []
lemma_by_id = {t.get('ID', '').strip(): t for t in tesauro_lemmata_csv if t.get('ID')}

# Indigenous peoples (pueblos indígenas): a thesaurus-like domain. Each row keys an
# optional HTML snippet in data/pueblos_indigenas/<peopleID>.html.
try:
    pueblos_csv = read_csv('data/pueblos_indigenas.csv')
except (FileNotFoundError, OSError):
    pueblos_csv = []
pueblos_by_id = {p.get('peopleID', '').strip(): p for p in pueblos_csv if p.get('peopleID')}


def _tesauro_lookup(slug):
    """Match a URL slug (spaces written as _) against ID / termino-es / term-en."""
    key = slug.replace('_', ' ').strip().lower()
    for t in tesauro_relaciones_csv:
        if (t.get('ID', '').strip() == slug
                or t.get('termino-es', '').strip().lower() == key
                or t.get('term-en', '').strip().lower() == key):
            return t
    return None


@bp.route('/tesauro')
def tesauro_index():
    lemmata = sorted(tesauro_lemmata_csv, key=lambda t: t.get('termino-es', '').lower())
    return render_template('tesauro_index.html', lemmata=lemmata)


@bp.route('/tesauro/<term>')
def tesauro_detail(term):
    row = _tesauro_lookup(term)
    lemma = lemma_by_id.get(term) or lemma_by_id.get(term.lower())
    parent = tesauro_by_id.get((row.get('parent') or '').strip()) if row else None
    children = [t for t in tesauro_relaciones_csv
                if row and (t.get('parent') or '').strip() == (row.get('ID') or '').strip()]
    related = []
    if row and row.get('related'):
        for rid in re.split(r'[;,@]', row['related']):
            rid = rid.strip()
            if rid and rid in tesauro_by_id and rid != (row.get('ID') or '').strip():
                related.append(tesauro_by_id[rid])

    # Term article HTML lives in data/tesauro/<slug>.html (files are lowercase).
    slug = term
    tdir = os.path.join(current_app.root_path, 'data', 'tesauro')
    article_exists = os.path.isfile(os.path.join(tdir, f'{slug}.html'))
    if not article_exists and os.path.isfile(os.path.join(tdir, f'{slug.lower()}.html')):
        slug = slug.lower()
        article_exists = True

    return render_template('tesauro_detail.html', term=term, term_slug=slug,
                           term_display=term.replace('_', ' '), row=row, lemma=lemma,
                           parent=parent, children=children, related=related,
                           article_exists=article_exists)


@bp.route('/data/tesauro/<path:filename>')
def tesauro_article(filename):
    directory = os.path.join(current_app.root_path, 'data', 'tesauro')
    if os.path.isfile(os.path.join(directory, filename)):
        return send_from_directory(directory, filename)
    abort(404)


@bp.route('/pueblos')
def pueblos_index():
    pueblos = sorted(pueblos_csv, key=lambda p: (p.get('nombre_pueblo') or '').lower())
    return render_template('pueblos_index.html', pueblos=pueblos)


@bp.route('/pueblos/<pid>')
def pueblos_detail(pid):
    row = pueblos_by_id.get(pid)
    if row is None:
        key = pid.replace('_', ' ').strip().lower()
        for p in pueblos_csv:
            if ((p.get('numID') or '').strip() == pid
                    or (p.get('nombre_pueblo') or '').strip().lower() == key):
                row = p
                break
    if row is None:
        abort(404)
    # Optional article HTML lives in data/pueblos_indigenas/<peopleID>.html.
    slug = (row.get('peopleID') or '').strip()
    pdir = os.path.join(current_app.root_path, 'data', 'pueblos_indigenas')
    article_exists = bool(slug) and os.path.isfile(os.path.join(pdir, f'{slug}.html'))
    return render_template('pueblos_detail.html', row=row, term_slug=slug,
                           article_exists=article_exists)


@bp.route('/data/pueblos_indigenas/<path:filename>')
def pueblos_article(filename):
    directory = os.path.join(current_app.root_path, 'data', 'pueblos_indigenas')
    if os.path.isfile(os.path.join(directory, filename)):
        return send_from_directory(directory, filename)
    abort(404)


@bp.route('/categoria/<gz_id>')
def category_detail(gz_id):
    place = None
    for p in gz_entidades_csv:
        if p['gz_id'] == gz_id:
            place = p
            break
    
    if place is None:
        return render_template('404.html'), 404

    category_data = [row for row in category_csv if row['gz_id'] == gz_id]
    
    if not category_data:
        return render_template('404.html'), 404

    return render_template('category_detail.html', place=place, category_data=category_data)

#######################
### Download routes ###
#######################
@bp.route('/territory/download', methods=['GET'])
def territory_download():
    # Extract search parameters
    name_query = request.args.get('name', '').strip()
    region = request.args.get('region', '').strip()
    tipo = request.args.get('tipo', '').strip()
    entidad = request.args.get('Entidad', '').strip()
    year_start = request.args.get('year_start', type=int)
    year_end = request.args.get('year_end', type=int)
    year = request.args.get('year', type=int)

    if year is not None:
        year_start = year_end = year

    # Normalize and split name terms
    name_terms = [normalize_string(term.strip()) for term in name_query.split('OR')] if name_query else []

    # Initial filter logic from entidades_csv (same as search)
    prefiltered = [
        t for t in entidades_csv if 
        (any(normalize_string(term) in normalize_string(t['Nombre']) or normalize_string(term) in normalize_string(t.get('Variantes', '')) for term in name_terms) if name_terms else True) and
        (region == '' or t['Region'] == region) and
        (tipo == '' or t['tipo'] == tipo) and
        (entidad == '' or t['Entidad'] == entidad)
    ]

    # Apply year filtering if specified
    if year_start is not None or year_end is not None:
        y_start = year_start if year_start is not None else -9999
        y_end = year_end if year_end is not None else 9999

        valid_entidades = set()
        for row in infotable_csv:
            try:
                row_start = int(float(row.get('START', -9999)))
                row_end = int(float(row.get('END_', 9999)))
                if row_end >= y_start and row_start <= y_end:
                    valid_entidades.add(row['Entidad'])
            except (ValueError, TypeError):
                continue

        filtered_results = [t for t in prefiltered if t['Entidad'] in valid_entidades]
    else:
        filtered_results = prefiltered

    # Now build the final output by pulling tipo and Label from infotable_csv
    final_rows = []
    
    for territory in filtered_results:
        entidad_id = territory['Entidad']
        
        # Find matching rows in infotable_csv for this Entidad
        matching_infotable_rows = [
            row for row in infotable_csv 
            if row['Entidad'] == entidad_id
        ]
        
        # If year filtering is active, apply temporal overlap to infotable rows
        if year_start is not None or year_end is not None:
            y_start = year_start if year_start is not None else -9999
            y_end = year_end if year_end is not None else 9999
            
            temporal_matching_rows = []
            for row in matching_infotable_rows:
                try:
                    row_start = int(float(row.get('START', -9999)))
                    row_end = int(float(row.get('END', 9999)))
                    if row_end >= y_start and row_start <= y_end:
                        temporal_matching_rows.append(row)
                except (ValueError, TypeError):
                    continue
            matching_infotable_rows = temporal_matching_rows
        
        # Create rows for each matching infotable entry
        if matching_infotable_rows:
            for info_row in matching_infotable_rows:
                final_rows.append({
                    'Entidad_ID': entidad_id,
                    'tipo': info_row.get('tipo', ''),
                    'Label': info_row.get('Label', ''),  # Changed from 'Nombre'
                    'REG': territory.get('Region', ''),
                    'START': info_row.get('START', ''),
                    'END_': info_row.get('END', ''),
                    'Nivel': territory.get('Nivel_default', '')  # Added Nivel from entidades_csv
                })
        else:
            # If no infotable match found, create a row with empty tipo and Label
            final_rows.append({
                'Entidad_ID': entidad_id,
                'tipo': '',
                'Label': '',
                'REG': territory.get('Region', ''),
                'START': '',
                'END_': '',
                'Nivel': territory.get('Nivel_default', '')
            })

    # Create CSV in memory
    output = StringIO()
    writer = csv.DictWriter(output, fieldnames=['Entidad_ID', 'tipo', 'Label', 'REG', 'START', 'END_', 'Nivel'], delimiter=';')
    writer.writeheader()
    for row in final_rows:
        writer.writerow(row)

    # Prepare response
    response = Response(output.getvalue(), mimetype='text/csv')
    response.headers.set("Content-Disposition", "attachment", filename="territory_results.csv")
    return response


@bp.route('/place/download', methods=['GET'])
def place_download():
    normalization_cache = {}

    def cached_normalize(text):
        if not text:
            return ''
        if text not in normalization_cache:
            normalization_cache[text] = normalize_string(text)
        return normalization_cache[text]

    # Retrieve search parameters
    name_query = request.args.get('name', '').strip()
    terr_name = request.args.get('terr_name', '').strip()
    terr_levels = [l.strip() for l in request.args.getlist('terr_level') if l.strip()]
    # Generic region narrows by the PLACE's own region code (not the territory's).
    place_regions = [r.strip() for r in request.args.getlist('region') if r.strip()]
    year_start = request.args.get('year_start', type=int)
    year_end = request.args.get('year_end', type=int)
    category_query = request.args.get('category', '').strip()
    subcategory_query = request.args.get('subcategory', '').strip()
    church_category = request.args.get('church_category', '').strip()
    religious_order = request.args.get('religious_order', '').strip()

    church_category_mappings = {
        'curatos': ['Curato', 'Parroquia', 'Vicaria', 'Sagrario'],
        'misiones': ['Mision capital', 'Mision cabecera', 'Mision'],
        'curatos_misiones': ['Curato', 'Parroquia', 'Vicaria', 'Sagrario',
                             'Mision capital', 'Mision cabecera', 'Mision'],
        'sede_obispado': ['Sagrario']
    }

    name_terms = [cached_normalize(term.strip()) for term in name_query.split('OR')] if name_query else []

    # Step 1: Initial filter by category, subcategory, church category, order, and temporal filtering
    matching_gz_ids = set()
    for info in gz_info_csv:
        if category_query and info.get('categoria') != category_query:
            continue
        if subcategory_query and info.get('categoria_especial') != subcategory_query:
            continue
        if church_category:
            if info.get('iglesia_cat') not in church_category_mappings.get(church_category, []):
                continue
        if religious_order and info.get('servido_por') != religious_order:
            continue
        
        # Apply temporal filtering to gz_info_csv
        if year_start is not None or year_end is not None:
            try:
                start = int(float(info.get('start', '0'))) if info.get('start') else 0
                end = int(float(info.get('end_', '9999'))) if info.get('end_') else 9999
                if year_start is not None and end < year_start:
                    continue
                if year_end is not None and start > year_end:
                    continue
            except (ValueError, TypeError):
                continue
        
        matching_gz_ids.add(info['gz_id'])

    # Step 2: Filter by name
    if name_terms:
        name_filtered_ids = set()
        for entity in gz_entidades_csv:
            if entity['gz_id'] not in matching_gz_ids:
                continue
            texts = [
                entity['nombre'].lower(),
                entity['label'].lower(),
                entity.get('variantes', '').lower(),
                cached_normalize(entity['nombre']).lower(),
                cached_normalize(entity['label']).lower(),
                cached_normalize(entity.get('variantes', '')).lower()
            ]
            if any(any(term.lower() in t for t in texts) for term in name_terms):
                name_filtered_ids.add(entity['gz_id'])
        matching_gz_ids = name_filtered_ids

    # Step 2b: Generic-region filter on the PLACE's own region code.
    if place_regions:
        rset = set(place_regions)
        matching_gz_ids = {gz for gz in matching_gz_ids if gz_region.get(gz) in rset}

    # Step 3: Belonging filter — mirror place_search (territory name/level)
    if terr_name or terr_levels:
        qnorm = normalize_string(terr_name).lower() if terr_name else ''
        match_ents = _territories_matching(qnorm, set(terr_levels))
        matching_gz_ids = _places_for_territories(match_ents, matching_gz_ids,
                                                  year_start, year_end)

    # Helper function to convert values with proper data types
    def format_value_with_type(key, value):
        if not value or value == '':
            return ''
            
        # Handle gz_id as int
        if key == 'gz_id':
            try:
                return int(value)
            except (ValueError, TypeError):
                return value
        
        # Handle start and end_ as int
        elif key in ['start', 'end_']:
            try:
                return int(float(value))
            except (ValueError, TypeError):
                return value
        
        # Handle lat and lon as float with comma delimiter
        elif key in ['lat', 'lon']:
            try:
                float_val = float(value)
                return str(float_val).replace('.', ',')
            except (ValueError, TypeError):
                return value
        
        # For other fields, use the original convert_decimal_commas logic
        else:
            return convert_decimal_commas(value)

    # Step 4: Build final data from gz_info_csv for matching gz_ids
    final_rows = []
    for row in gz_info_csv:
        if row.get('gz_id') in matching_gz_ids:
            formatted_row = {}
            for key in ['gz_id', 'label', 'nombre', 'categoria', 'categoria_especial', 
                       'iglesia_cat', 'servido_por', 'start', 'start_ex', 'end_', 
                       'end_ex', 'lat', 'lon', 'cert']:
                formatted_row[key] = format_value_with_type(key, row.get(key, ''))
            final_rows.append(formatted_row)

    # Create CSV with the requested fields
    output = StringIO()
    fieldnames = ['gz_id', 'label', 'nombre', 'categoria', 'categoria_especial', 
                  'iglesia_cat', 'servido_por', 'start', 'start_ex', 'end_', 
                  'end_ex', 'lat', 'lon', 'cert']
    writer = csv.DictWriter(output, fieldnames=fieldnames, delimiter=';')
    writer.writeheader()
    for row in final_rows:
        writer.writerow(row)

    # Return CSV response
    response = Response(output.getvalue(), mimetype='text/csv')
    response.headers.set("Content-Disposition", "attachment", filename="place_results.csv")
    return response


#####################
### Helper routes ###
#####################
@bp.route('/get_subcategories/<category>')
def get_subcategories(category):
    # Get unique subcategories for the selected category
    subcategories = sorted(list(set(
        info['categoria_especial'] for info in gz_info_csv 
        if info.get('categoria') == category and info.get('categoria_especial')
    )))
    return jsonify(subcategories)

@bp.route('/data/historia_gz_entidades/<path:filename>')
def historia_lugar(filename):
    directory = os.path.join(current_app.root_path, 'data', 'historia_gz_entidades')
    if os.path.isfile(os.path.join(directory, filename)):
        return send_from_directory(directory, filename)
    else:
        abort(404)
@bp.route('/data/historia_entidades/<path:filename>')
def historia_territorio(filename):
    directory = os.path.join(current_app.root_path, 'data', 'historia_entidades')
    if os.path.isfile(os.path.join(directory, filename)):
        return send_from_directory(directory, filename)
    else:
        abort(404)

@bp.route('/data/historia_instituciones/<path:filename>')
def historia_institucion(filename):
    directory = os.path.join(current_app.root_path, 'data', 'historia_instituciones')
    if os.path.isfile(os.path.join(directory, filename)):
        return send_from_directory(directory, filename)
    else:
        abort(404)


