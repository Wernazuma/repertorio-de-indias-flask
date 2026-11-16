"""Full citations and RIS export for rows of the source table (data/fuentes.csv).

The table follows the RIS source template (data/guidebooks/
ris_source_fields_by_type.csv): one row per source, `doc_type` one of the
document-type labels of the Types sheet, people as "Surname, Forename" joined
with '; ', dates as YYYY/MM/DD.

format_citation(row, lang) -> Markup   (house style: "Forename Surname, Title (Place: Publisher Year)")
to_ris(row)                -> str      (one RIS record, CRLF line endings)
"""
import re

from markupsafe import Markup, escape

# doc_type labels (Types sheet)
ARCH_SER, ARCH_DOC = 'Archival series/holding', 'Archival document'
HIST_MAP, MOD_MAP = 'Historical map (before 1824)', 'Modern map (after 1824)'
PRINT, CRIT = 'Print', 'Critical edition'
FINDING, MONO, EDVOL = 'Archival catalogue/finding aid', 'Monograph', 'Edited volume'
CHAP, ART, THES = 'Chapter', 'Article', 'Thesis'
ONL_S, DBASE, ONL_O = 'Scholarly online source', 'Database', 'Other online source'

_L = {  # key -> (es, en)
    'in': ('en', 'in'),
    'ed': ('ed.', 'ed.'),
    'eds': ('eds.', 'eds.'),
    'ed_by': ('ed. de', 'ed. by'),
    'trans': ('trad.', 'trans.'),
    'vol': ('vol.', 'vol.'),
    'vols': ('vols.', 'vols.'),
    'p': ('p.', 'p.'),
    'pp': ('pp.', 'pp.'),
    'f': ('f.', 'fol.'),
    'ff': ('ff.', 'fols.'),
    'scale': ('escala', 'scale'),
    'orig': ('orig.', 'orig.'),
    'thesis': ('Tesis', 'Thesis'),
    'accessed': ('consultado el', 'accessed'),
    'no': ('n.º', 'no.'),
}


def _l(key, lang):
    es, en = _L[key]
    return es if lang == 'es' else en


def _v(row, key):
    return (row.get(key) or '').strip()


def _people(s):
    return [p.strip() for p in (s or '').split(';') if p.strip()]


def _forename_first(name):
    """'Surname, Forename' -> 'Forename Surname' (institutions stay as they are)."""
    if ',' in name:
        sur, fore = name.split(',', 1)
        if fore.strip():
            return '{} {}'.format(fore.strip(), sur.strip())
        return sur.strip()
    return name


def _names(s):
    return ' / '.join(_forename_first(p) for p in _people(s))


def _year(row):
    return _v(row, 'date_text') or _v(row, 'year')


def _imprint(row, lang, with_year=True):
    """'(Place: Publisher Year)' -- the parts present, in the legacy house style."""
    place, pub, yr = _v(row, 'place'), _v(row, 'publisher'), _year(row) if with_year else ''
    head = ': '.join(x for x in (place, pub) if x)
    inner = ' '.join(x for x in ((head + ',') if head and yr and pub else head, yr) if x)
    return '({})'.format(inner) if inner else ''


def _pages(row, lang, folios=False):
    a, b = _v(row, 'page_start'), _v(row, 'page_end')
    if not a:
        return ''
    if re.search(r'[A-Za-z]', a) and not b:      # an extent or folio note as written
        return a
    if b and b != a:
        return '{} {}–{}'.format(_l('ff' if folios else 'pp', lang), a, b)
    return '{} {}'.format(_l('f' if folios else 'p', lang), a)


def _volumes(row, lang):
    vol, nv = _v(row, 'volume'), _v(row, 'num_volumes')
    if vol:
        return '{} {}'.format(_l('vol', lang), vol)
    if nv and nv != '1':
        return '{} {}'.format(nv, _l('vols', lang))
    return ''


def _editors_suffix(eds, lang):
    return '{} ({})'.format(_names(eds), _l('eds' if len(_people(eds)) > 1 else 'ed', lang))


def _i(text):
    return Markup('<i>{}</i>').format(text)


def _q(text):
    return Markup('‘{}’').format(text)


def _join(parts, sep=', '):
    parts = [p for p in parts if p]
    out = Markup('')
    for n, p in enumerate(parts):
        out += (Markup(escape(sep)) if n else Markup('')) + escape(p)
    return out


def _tail(row, lang):
    bits = []
    if _v(row, 'doi'):
        bits.append(Markup('DOI: {}').format(_v(row, 'doi')))
    url = _v(row, 'url')
    if url:
        acc = _v(row, 'accessed')
        link = Markup('<a href="{0}" target="_blank" rel="noopener">{0}</a>').format(url)
        if acc:
            link += Markup(' ({} {})').format(_l('accessed', lang), acc.replace('/', '-'))
        bits.append(link)
    return bits


def _book_head(row, lang):
    """Author(s), or editor(s) with (ed./eds.) when the book has no author."""
    if _v(row, 'author'):
        return _names(_v(row, 'author'))
    if _v(row, 'editor'):
        return _editors_suffix(_v(row, 'editor'), lang)
    return ''


def _container(row, lang, journal=False):
    """'in: Editors (eds.), Container vol. (Place: Publisher Year), pp.' """
    ct = _v(row, 'container_title')
    if journal:
        num = ', '.join(x for x in (_v(row, 'volume'), _v(row, 'issue')) if x)
        s = _i(ct) + (' ' + num if num else '')
        yr = _year(row)
        if yr:
            s += ' ({})'.format(yr)
        return _join([Markup('{}: ').format(_l('in', lang)) + s, _pages(row, lang)])
    parts = []
    eds = _v(row, 'editor')
    lead = Markup('{}: ').format(_l('in', lang))
    if eds and _v(row, 'author'):
        lead += _editors_suffix(eds, lang) + ', '
    parts.append(lead + _i(ct))
    parts.append(_volumes(row, lang))
    body = _join(parts)
    imp = _imprint(row, lang)
    if _v(row, 'series_title'):
        body += ' ({})'.format(_v(row, 'series_title'))
    if imp:
        body += ' ' + imp
    return _join([body, _pages(row, lang)])


def _book(row, lang, title_fmt=_i):
    head = _book_head(row, lang)
    title = title_fmt(_v(row, 'title')) if _v(row, 'title') else ''
    extra = []
    if _v(row, 'edition'):
        extra.append(_v(row, 'edition'))
    if _v(row, 'author') and _v(row, 'editor'):
        extra.append('{} {}'.format(_l('ed_by', lang), _names(_v(row, 'editor'))))
    if _v(row, 'translator'):
        extra.append('{} {}'.format(_l('trans', lang), _names(_v(row, 'translator'))))
    extra.append(_volumes(row, lang))
    if _v(row, 'scale'):
        extra.append('{} {}'.format(_l('scale', lang), _v(row, 'scale')))
    body = _join([head, title] + extra)
    if _v(row, 'series_title'):
        body += ' ({})'.format(_v(row, 'series_title'))
    if row.get('doc_type') == THES:
        uni = ', '.join(x for x in (_v(row, 'publisher'), _v(row, 'place')) if x)
        inner = ', '.join(x for x in (_l('thesis', lang), uni) if x)
        inner = ' '.join(x for x in (inner, _year(row)) if x)
        body += ' ({})'.format(inner)
    else:
        imp = _imprint(row, lang)
        if imp:
            body += ' ' + imp
    if _v(row, 'original_pub'):
        body += ' [{} {}]'.format(_l('orig', lang), _v(row, 'original_pub'))
    pages = _pages(row, lang)
    return _join([body, pages] if row.get('doc_type') in (PRINT, CRIT, HIST_MAP) else [body])


def _holding(row, lang):
    """'Archive (City), Fonds, Shelfmark, fols.'"""
    arch = _v(row, 'archive')
    if arch and _v(row, 'place'):
        arch = '{} ({})'.format(arch, _v(row, 'place'))
    return _join([arch, _v(row, 'container_title'), _v(row, 'shelfmark'),
                  _pages(row, lang, folios=True)])


def _archival(row, lang):
    head = _names(_v(row, 'author'))
    title, yr = _v(row, 'title'), _year(row)
    if yr and re.sub(r'\D', '', yr)[:4] in title:      # date already part of the title
        yr = ''
    desc = _join([head, title, yr])
    if _v(row, 'scale'):
        desc = _join([desc, '{} {}'.format(_l('scale', lang), _v(row, 'scale'))])
    hold = _holding(row, lang)
    return desc + ('. ' + hold if hold else '') if desc else hold


def format_citation(row, lang='es'):
    """The full citation of a source row as safe HTML ('' when the row is empty)."""
    if not any(_v(row, k) for k in ('title', 'author', 'editor', 'url', 'shelfmark')):
        return Markup('')
    dt = row.get('doc_type') or ''
    has_ct = bool(_v(row, 'container_title'))
    if dt in (ARCH_DOC, ARCH_SER):
        cit = _archival(row, lang)
    elif dt == HIST_MAP and _v(row, 'archive') and not _v(row, 'publisher'):
        cit = _archival(row, lang)           # map held in an archive: container_title is the fonds
    elif dt == ART:
        cit = _join([_names(_v(row, 'author')), _q(_v(row, 'title')), _container(row, lang, journal=True)])
    elif dt == CHAP or (dt in (CRIT, PRINT, HIST_MAP, MOD_MAP) and has_ct):
        head = _names(_v(row, 'author'))
        cit = _join([head, _q(_v(row, 'title')), _container(row, lang)])
        if dt == HIST_MAP and _v(row, 'archive'):
            cit += '. ' + _holding(row, lang)
    elif dt in (ONL_S, ONL_O, DBASE):
        title = _i(_v(row, 'title')) if dt == DBASE or not has_ct else _q(_v(row, 'title'))
        parts = [_names(_v(row, 'author') or _v(row, 'editor')), title]
        if has_ct and _v(row, 'container_title') != _v(row, 'title'):
            parts.append(Markup('{}: ').format(_l('in', lang)) + _i(_v(row, 'container_title')))
        if _v(row, 'edition'):
            parts.append(_v(row, 'edition'))
        if _v(row, 'publisher') and _v(row, 'publisher') != _v(row, 'container_title'):
            parts.append(_v(row, 'publisher'))
        parts.append(_v(row, 'date').replace('/', '-') or _year(row))
        cit = _join(parts)
    else:   # monograph, edited volume, finding aid, thesis, print, critical edition, maps
        cit = _book(row, lang)
        if dt in (PRINT, HIST_MAP) and _v(row, 'archive'):
            cit += '. ' + _holding(row, lang)
    tail = _tail(row, lang)
    cit = Markup(cit or escape(_v(row, 'title')))
    if tail:
        cit = cit.rstrip('. ') + Markup('. ') + _join(tail, '. ')
    cit = Markup(cit)
    return cit if cit.endswith('.') else cit + Markup('.')


# ------------------------------------------------------------------ RIS ----
# editor tag: A2 is what reference managers read as "editor" for books and
# chapters alike (A3 is imported as series editor by Zotero / EndNote).
_SIMPLE = [('title', 'TI'), ('short_title', 'ST'), ('volume', 'VL'), ('num_volumes', 'NV'),
           ('issue', 'IS'), ('edition', 'ET'), ('place', 'CY'), ('publisher', 'PB'),
           ('page_start', 'SP'), ('page_end', 'EP'), ('scale', 'M1'), ('archive', 'DB'),
           ('shelfmark', 'AN'), ('original_pub', 'OP'), ('language', 'LA'),
           ('isbn_issn', 'SN'), ('doi', 'DO'), ('url', 'UR'), ('accessed', 'Y2')]


def _ris_line(tag, val):
    val = re.sub(r'\s*[\r\n]+\s*', ' ', str(val)).strip()
    return '{}  - {}'.format(tag, val) if val else None


def to_ris(row):
    """One RIS record for a source row."""
    lines = [_ris_line('TY', _v(row, 'ris_type') or 'GEN'),
             _ris_line('ID', _v(row, 'fuenteID') or _v(row, 'FuenteID'))]
    for a in _people(row.get('author')):
        lines.append(_ris_line('AU', a))
    for e in _people(row.get('editor')):
        lines.append(_ris_line('A2', e))
    for tr in _people(row.get('translator')):
        lines.append(_ris_line('A4', tr))
    for key, tag in _SIMPLE[:1]:
        lines.append(_ris_line(tag, _v(row, key)))
    ct, st = _v(row, 'container_title'), _v(row, 'series_title')
    if ct:
        lines.append(_ris_line('T2', ct))
        if st:
            lines.append(_ris_line('T3', st))
    elif st:
        lines.append(_ris_line('T3' if row.get('doc_type') == CHAP else 'T2', st))
    if _v(row, 'year'):
        lines.append(_ris_line('PY', _v(row, 'year')))
    if _v(row, 'date'):
        lines.append(_ris_line('DA', _v(row, 'date')))
    elif _v(row, 'date_text'):
        lines.append(_ris_line('DA', _v(row, 'date_text')))
    for key, tag in _SIMPLE[1:]:
        lines.append(_ris_line(tag, _v(row, key)))
    for kw in _people(row.get('keywords')):
        lines.append(_ris_line('KW', kw))
    if _v(row, 'date') and _v(row, 'date_text'):
        lines.append(_ris_line('N1', 'Date as given: ' + _v(row, 'date_text')))
    if _v(row, 'notes'):
        lines.append(_ris_line('N1', _v(row, 'notes')))
    lines.append('ER  - ')
    return '\r\n'.join(l for l in lines if l) + '\r\n'
