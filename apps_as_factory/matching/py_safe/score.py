# main/matching/score.py
def decide_phase_outcome(toponym_type: str,
                         saint_status: str,
                         category_status: str,
                         fuzzy_score: float) -> str:
    """
    Rule-based Phase 1a classification,
    fully aligned with the original matching logic.
    """

    # --- AUTO ADOPT ---
    if (
        # (A) exact name match
        toponym_type in ("toponym_label", "toponym_nombre")
        and saint_status in ("saint_match", "no_saint")
        and category_status in ("category_match", "category_null")
    ):
        return "auto_adopt"

    # (B) fuzzy but extremely strong match (≥0.9)
    if (
        fuzzy_score >= 0.9
        and saint_status in ("saint_match", "no_saint")
        and category_status in ("category_match", "category_null")
    ):
        return "auto_adopt"

    # --- CANDIDATE ---
    if fuzzy_score >= 0.85:
        return "candidate"

    # --- RELEGATED ---
    return "relegated"
