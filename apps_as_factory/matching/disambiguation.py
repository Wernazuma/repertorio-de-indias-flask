import math

import pandas as pd

# Place importance, most important first. Used only to break score ties so the
# more significant settlement is offered at the top of the candidate list.
IMPORTANCE_ORDER = ["Ciudad", "Villa", "Pueblo", "Poblacion", "Rural", "Fuerte", "Localidad"]


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


def disambiguate_candidates(df: pd.DataFrame) -> pd.DataFrame:
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

        # Proximity: when the input row carries coordinates (ref_lat / ref_lon),
        # compute each candidate's great-circle distance to them. This becomes
        # the PRIMARY ranking key, so the geographically closest candidate is
        # offered first. Candidates (or inputs) without usable coordinates get a
        # NaN distance and sort last, falling back to score / importance.
        in_lat = group["ref_lat"].iloc[0] if "ref_lat" in group.columns else None
        in_lon = group["ref_lon"].iloc[0] if "ref_lon" in group.columns else None
        has_input_coords = _coord(in_lat) is not None and _coord(in_lon) is not None
        if has_input_coords and "lat" in group.columns and "lon" in group.columns:
            group["distance_km"] = group.apply(
                lambda r: _haversine_km(in_lat, in_lon, r.get("lat"), r.get("lon")),
                axis=1,
            )
        else:
            group["distance_km"] = pd.NA

        # Break remaining ties by place importance (Ciudad > Villa > … ) so the
        # weightier settlement is offered first when distance/score are equal.
        group["_importance"] = group.get("lugar_categoria", "").apply(_importance_rank) \
            if "lugar_categoria" in group.columns else 99
        group = group.sort_values(
            ["distance_km", "score", "_importance"],
            ascending=[True, False, True],
            na_position="last",
        ).drop(columns=["_importance"])
        decisions.append(group)

    if not decisions:
        return pd.DataFrame(columns=list(df.columns) + ["score", "distance_km"])

    return pd.concat(decisions, ignore_index=True)
