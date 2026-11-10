import math
import os
import re

import pandas as pd
from rapidfuzz import fuzz

from .cleaning import clean_toponym
from .saints import load_saints, extract_saint

# Place importance, most important first. Used only to break score ties so the
# more significant settlement is offered at the top of the candidate list.
IMPORTANCE_ORDER = ["Ciudad", "Villa", "Pueblo", "Poblacion", "Rural", "Fuerte", "Localidad"]

# Candidates whose distance to the input's expected location falls in the SAME
# band of this width are ordered by name-match score (so the better toponym wins
# among nearby places); only across bands does the nearer one win outright (so a
# far-off container still sinks). Tunable.
PROXIMITY_BUCKET_KM = 250

# Devotional / connective fillers stripped when isolating a toponym's core.
_CORE_FILLER = {"de", "del", "la", "las", "los", "el", "y", "san", "santa",
                "santo", "santos", "sant", "nuestra", "senora", "senor"}

_SAINTS = None


def _saints():
    """Cleaned saints list, loaded once (same santos.csv the matcher uses)."""
    global _SAINTS
    if _SAINTS is None:
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "data", "santos.csv")
        _SAINTS = load_saints(path)
    return _SAINTS


def _input_core(label):
    """The distinctive (non-saint, non-filler) part of an input toponym, cleaned.

    'San Pedro de Chipao' -> 'chipao' (the identifying place name);
    'San Pedro'           -> ''       (a pure saint dedication, no core).
    """
    t = clean_toponym(str(label or ""))
    if not t:
        return ""
    saint = extract_saint(t, _saints())
    if saint:
        t = t.replace(saint, " ")
    toks = [tok for tok in re.split(r"[\s@]+", t) if tok and tok not in _CORE_FILLER]
    return " ".join(toks).strip()


def _candidate_matches_core(row, core):
    """True when a candidate's name carries the input's distinctive core toponym
    (exact token, substring, or a near spelling)."""
    if not core or len(core) < 3:
        return False
    names = []
    for col in ("lugar_label", "lugar_nombre", "lugar_variantes"):
        v = row.get(col)
        if v is not None and str(v).strip():
            names.extend(t for t in re.split(r"[\s@]+", clean_toponym(str(v))) if t)
    if not names:
        return False
    joined = " ".join(names)
    if core in joined:
        return True
    return (fuzz.partial_ratio(core, joined) >= 90
            or any(fuzz.partial_ratio(core, n) >= 88 for n in names))


def _coord(v):
    """Parse a coordinate (float, or a string that may use ',' as the decimal
    separator) into a float in a plausible range, or None."""
    if v is None:
        return None
    try:
        if pd.isna(v):
            return None
    except (TypeError, ValueError):
        pass
    try:
        return float(str(v).strip().replace(",", "."))
    except (TypeError, ValueError):
        return None


def _haversine_km(lat1, lon1, lat2, lon2):
    """Great-circle distance in km between two points, or None if any
    coordinate is missing or out of range."""
    a1, o1, a2, o2 = _coord(lat1), _coord(lon1), _coord(lat2), _coord(lon2)
    if None in (a1, o1, a2, o2):
        return None
    if not (-90 <= a1 <= 90 and -90 <= a2 <= 90 and -180 <= o1 <= 180 and -180 <= o2 <= 180):
        return None
    r = 6371.0088  # mean Earth radius, km
    p1, p2 = math.radians(a1), math.radians(a2)
    dphi = math.radians(a2 - a1)
    dlmb = math.radians(o2 - o1)
    h = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(h)))


def _importance_rank(categoria):
    cat = str(categoria or "").strip()
    return IMPORTANCE_ORDER.index(cat) if cat in IMPORTANCE_ORDER else len(IMPORTANCE_ORDER)


def score_row(row):
    score = 0
    score += {
        "toponym_nombre": 5,
        "toponym_label": 4,
        "toponym_variante": 3,
        "fuzzy": 1,
        "none": 0
    }.get(str(row.get("toponym_match", "")), 0)

    score += {
        "saint_match": 5,
        "no_saint": 1,
        "saint_mismatch": 0
    }.get(str(row.get("saint_match", "")), 0)

    # Settlement type is a strong signal when the input provides it: a matching
    # category should float a candidate well above name-only rivals. Weighted
    # above toponym (max 5) / saint (max 5) so a category hit dominates ties.
    score += {
        "category_match": 6,
        "category_fuzzy": 4,
        "category_null": 1,
        "category_mismatch": 0
    }.get(str(row.get("category_match", "")), 0)

    iglesia_value = str(row.get("lugar_iglesia_cat", "")).strip()
    if iglesia_value in ["Curato", "Sagrario", "Mision cabecera"]:
        score += 3

    return score


def disambiguate_candidates(df: pd.DataFrame, ref_point_fn=None) -> pd.DataFrame:
    """Rank each rowID's candidates for the manual disambiguation screen.

    ``ref_point_fn(group_first_row) -> (lat, lon) | None`` supplies the location
    the input row is expected to sit near (its own coordinates, or the centroid
    of the territory it names). When given, candidates are ranked by great-circle
    distance to that point FIRST (proximity-primary), so a place in a neighbouring
    container outranks an equally-named one far away. Without it, the explicit
    ref_lat / ref_lon columns are used if present, else distance is unavailable
    and ranking falls back to score / importance.
    """
    candidates = df[df["phase_1a_outcome"] == "candidate"].copy()
    grouped = candidates.groupby("rowID")
    decisions = []

    for rowID, group in grouped:
        group = group.drop_duplicates(subset=["gz_id"], keep="first").copy()

        # Prefer stronger toponym matches if available
        strong = {"toponym_nombre", "toponym_label"}
        if group["toponym_match"].isin(strong).any():
            group = group[group["toponym_match"].isin(strong)].copy()

        # Drop saint mismatches if any saint match exists
        if (group["saint_match"] == "saint_match").any():
            group = group[group["saint_match"] != "saint_mismatch"].copy()

        group["score"] = group.apply(score_row, axis=1)

        # Core-toponym vs saint-coincidence. For an input like "San Pedro de
        # Chipao" the distinctive name is "Chipao"; a candidate carrying that core
        # is far more identifying than one agreeing only on the saint dedication
        # ("San Pedro" is everywhere). Reward a core match, and neutralise a saint
        # bonus that rests on nothing but a coincidental saint agreement.
        core = _input_core(group["ref_Label"].iloc[0]) if "ref_Label" in group.columns else ""

        def _name_adjust(r):
            if not core:
                return 0
            if _candidate_matches_core(r, core):
                return 8
            if str(r.get("saint_match", "")) == "saint_match":
                return -4
            return 0

        if core:
            group["score"] = group["score"] + group.apply(_name_adjust, axis=1)

        # Proximity: the reference point is where the input row is expected to
        # sit — its own coordinates, or (via ref_point_fn) the centroid of the
        # territory it names. Each candidate's great-circle distance to it is the
        # PRIMARY ranking key, so the geographically closest candidate is offered
        # first and a far-off container sinks to the bottom. Candidates (or inputs)
        # without a usable distance get NaN and sort last, falling back to
        # score / importance.
        ref_pt = ref_point_fn(group.iloc[0]) if ref_point_fn is not None else None
        if ref_pt is None:
            il = _coord(group["ref_lat"].iloc[0]) if "ref_lat" in group.columns else None
            io = _coord(group["ref_lon"].iloc[0]) if "ref_lon" in group.columns else None
            ref_pt = (il, io) if (il is not None and io is not None) else None
        if ref_pt is not None and "lat" in group.columns and "lon" in group.columns:
            dist = group.apply(
                lambda r: _haversine_km(ref_pt[0], ref_pt[1], r.get("lat"), r.get("lon")),
                axis=1,
            )
            # Keep a consistent float64 dtype (the apply yields a float/None object
            # column); an object or all-NA column makes pd.concat below warn and, in
            # future pandas, change result dtypes.
            group["distance_km"] = pd.to_numeric(dist, errors="coerce")
        else:
            group["distance_km"] = float("nan")

        # Proximity BAND, not raw distance, is the primary key: candidates within
        # the same ~PROXIMITY_BUCKET_KM band are then ordered by name score (so the
        # better toponym wins among nearby places — e.g. Chipao over a same-area
        # San Pedro), while a far-off container lands in a higher band and sinks.
        # No usable distance -> last band, ranked on name score alone.
        def _band(d):
            return int(d // PROXIMITY_BUCKET_KM) if pd.notna(d) else 10 ** 6
        group["_band"] = group["distance_km"].apply(_band)

        # Break remaining ties by place importance (Ciudad > Villa > … ).
        group["_importance"] = group.get("lugar_categoria", "").apply(_importance_rank) \
            if "lugar_categoria" in group.columns else 99
        group = group.sort_values(
            ["_band", "score", "distance_km", "_importance"],
            ascending=[True, False, True, True],
            na_position="last",
        ).drop(columns=["_importance", "_band"])
        decisions.append(group)

    if not decisions:
        return pd.DataFrame(columns=list(df.columns) + ["score", "distance_km"])

    return pd.concat(decisions, ignore_index=True)
