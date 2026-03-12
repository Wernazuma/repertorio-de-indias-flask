# disambiguation.py

import pandas as pd

# --- Scoring function ---
def score_row(row):
    score = 0

    # Toponym match
    score += {
        "toponym_nombre": 5,
        "toponym_label": 4,
        "toponym_variante": 3,
        "toponym_partial": 1,
        "toponym_levenshtein": 1
    }.get(row["toponym-match"], 0)

    # Saint match
    score += {
        "saint_match": 5,
        "saint_null": 1,
        "saint_mismatch": 0
    }.get(row["saint-match"], 0)

    # Category match
    score += {
        "category_match": 2,
        "category_null": 1,
        "category_mismatch": 0
    }.get(row["category-match"], 0)

    # Iglesia category values
    iglesia_value = str(row.get("lugar_iglesia_cat", "")).strip()
    if iglesia_value in ["Curato", "Sagrario", "Mision cabecera"]:
        score += 3

    return score


# --- Disambiguation pipeline ---
def disambiguate_candidates(df: pd.DataFrame) -> pd.DataFrame:
    """
    Takes a merged DataFrame (Phase 1 results enriched with gazetteer fields),
    and returns disambiguated candidate sets ready for manual review.
    """
    grouped = df[df["phase-1-outcome"] == "candidate"].groupby("rowID")
    decisions = []

    for rowID, group in grouped:
        group = group.drop_duplicates(subset=["gz_id"], keep="first")

        # Eliminate partial or levenshtein if stronger match exists
        if any(group["toponym-match"].isin(["toponym_nombre", "toponym_label"])):
            group = group[~group["toponym-match"].isin(["toponym_partial", "toponym_levenshtein"])]

        # Eliminate saint mismatches if any saint_match is present
        if "saint_match" in group["saint-match"].values:
            group = group[group["saint-match"] != "saint_mismatch"]

        # Always score and present for manual review
        group["score"] = group.apply(score_row, axis=1)
        group["phase-1-outcome"] = "manual_review"
        group = group.sort_values("score", ascending=False)

        decisions.append(group)

    if not decisions:
        return pd.DataFrame(columns=df.columns.tolist() + ["score"])

    return pd.concat(decisions, ignore_index=True)

