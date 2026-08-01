import pandas as pd

# Place importance, most important first. Used only to break score ties so the
# more significant settlement is offered at the top of the candidate list.
IMPORTANCE_ORDER = ["Ciudad", "Villa", "Pueblo", "Poblacion", "Rural", "Fuerte", "Localidad"]


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
        # Sort by score; break ties by place importance (Ciudad > Villa > … ) so
        # the weightier settlement is offered first when scores are equal.
        group["_importance"] = group.get("lugar_categoria", "").apply(_importance_rank) \
            if "lugar_categoria" in group.columns else 99
        group = group.sort_values(["score", "_importance"], ascending=[False, True]) \
                     .drop(columns=["_importance"])
        decisions.append(group)

    if not decisions:
        return pd.DataFrame(columns=list(df.columns) + ["score"])

    return pd.concat(decisions, ignore_index=True)
