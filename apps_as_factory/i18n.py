# i18n.py — lightweight bilingual (English / Spanish) support for the whole site.
#
# Two layers:
#   t(key)   → UI-chrome strings, from translations/{lang}.json (fallback: en → key)
#   vt(term) → controlled-vocabulary terms (Spanish in the data), from translations/
#              vocab.csv (es;en). Spanish is the canonical value; English is display-only.
#
# Locale resolution (see resolve_locale):
#   1. explicit `arca_lang` cookie (set by the EN|ES toggle) wins;
#   2. otherwise the browser's top Accept-Language: es* → Spanish, anything else → English.
#
# Spanish stays the canonical index/search language everywhere; this module only affects
# display. Search / matching logic must never branch on the active locale.

import csv
import json
from pathlib import Path

from flask import request, has_request_context, make_response, redirect

_DIR = Path(__file__).resolve().parent / "translations"
LANGS = ("en", "es")
DEFAULT_LANG = "en"
LANG_COOKIE = "arca_lang"

_ui = {}      # {lang: {key: text}}
_vocab = {}   # {spanish_term: english_term}


def _load():
    global _ui, _vocab
    _ui = {}
    for lang in LANGS:
        p = _DIR / f"{lang}.json"
        _ui[lang] = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    _vocab = {}
    vp = _DIR / "vocab.csv"
    if vp.exists():
        with open(vp, encoding="utf-8") as f:
            for row in csv.DictReader(f, delimiter=";"):
                es = (row.get("es") or "").strip()
                en = (row.get("en") or "").strip()
                if es and en:
                    _vocab[es] = en


_load()


def resolve_locale():
    if not has_request_context():
        return DEFAULT_LANG
    cookie = request.cookies.get(LANG_COOKIE)
    if cookie in LANGS:
        return cookie
    langs = list(request.accept_languages)  # sorted by quality, highest first
    if langs and langs[0][0].lower().startswith("es"):
        return "es"
    return DEFAULT_LANG


def t(key, lang=None, **kw):
    lang = lang or resolve_locale()
    val = _ui.get(lang, {}).get(key)
    if val is None:
        val = _ui.get(DEFAULT_LANG, {}).get(key, key)
    if kw:
        try:
            val = val.format(**kw)
        except (KeyError, IndexError, ValueError):
            pass
    return val


def vt(term, lang=None):
    if not term:
        return term
    lang = lang or resolve_locale()
    if lang == "es":
        return term
    return _vocab.get(term.strip(), term)


# Sentinel years used in the HGIS data.
_YEAR_START_UNKNOWN = 1111   # "from the beginning / unknown start"
_YEAR_END_OPEN = 9999        # "still open / unknown end"
_YEAR_STUDY_END = 1808       # end of the study period -> flagged with "*"


def _to_year(v):
    if v is None:
        return None
    s = str(v).strip()
    if s == "":
        return None
    try:
        return int(float(s))
    except (ValueError, TypeError):
        return None


def timespan(start, end, lang=None):
    """Render a (start, end) year pair with the HGIS sentinel conventions:

    1111-YYYY -> "until YYYY" / "hasta YYYY"
    YYYY-9999 -> "YYYY+"
    1111-9999 -> "always" / "siempre"
    end == 1808 (study-period end) -> the year is suffixed with "*"
    """
    lang = lang or resolve_locale()
    s = _to_year(start)
    e = _to_year(end)

    if s is None and e is None:
        # unparseable / empty -> fall back to a plain raw rendering
        raw_s = "" if start is None else str(start).strip()
        raw_e = "" if end is None else str(end).strip()
        if not raw_s and not raw_e:
            return ""
        return "{}-{}".format(raw_s, raw_e)

    def end_str(y):
        return "{}*".format(y) if y == _YEAR_STUDY_END else str(y)

    s_unknown = s == _YEAR_START_UNKNOWN
    e_open = e == _YEAR_END_OPEN

    if s_unknown and e_open:
        return t("ts_always", lang=lang)
    if s_unknown and e is not None:
        return "{} {}".format(t("ts_until", lang=lang), end_str(e))
    if e_open and s is not None:
        return "{}+".format(s)
    if s is not None and e is not None:
        return "{}-{}".format(s, end_str(e))
    if s is not None:
        return str(s)
    return end_str(e)


def init_app(app):
    """Wire the language toggle route and inject t / vt / lang into all templates."""

    @app.context_processor
    def _inject_i18n():
        lang = resolve_locale()
        return {
            "lang": lang,
            "LANGS": LANGS,
            "t": lambda key, **kw: t(key, lang=lang, **kw),
            "vt": lambda term: vt(term, lang=lang),
            "timespan": lambda start, end: timespan(start, end, lang=lang),
        }

    @app.route("/lang/<code>")
    def set_language(code):
        dest = request.referrer or "/"
        resp = make_response(redirect(dest))
        if code in LANGS:
            resp.set_cookie(LANG_COOKIE, code, max_age=31536000, samesite="Lax")
        return resp
