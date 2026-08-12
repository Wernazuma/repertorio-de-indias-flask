# main/matching/score.py
def decide_phase_outcome(toponym_type: str,
                         saint_status: str,
                         category_status: str,
                         fuzzy_score: float) -> str:
    """
    Rule-based Phase 1a classification,
    fully aligned with the original matching logic.
    """

    # A fuzzy category match is acceptable for auto-adopt; only an outright
    # category_mismatch blocks it (the input type contradicts the gazetteer).
    category_ok = category_status in ("category_match", "category_fuzzy", "category_null")

    # --- AUTO ADOPT ---
    if (
        # (A) exact name match
        toponym_type in ("toponym_label", "toponym_nombre")
        and saint_status in ("saint_match", "no_saint")
        and category_ok
    ):
        return "auto_adopt"

    # (B) fuzzy but extremely strong match (≥0.9)
    if (
        fuzzy_score >= 0.9
        and saint_status in ("saint_match", "no_saint")
        and category_ok
    ):
        return "auto_adopt"

    # A confirmed different-saint advocation is never the same place, however
    # high the raw fuzzy score: partial_ratio rewards the shared "San …" prefix,
    # so "San Diego" scores ~87 against "San Dionisio". saint_status is only
    # "saint_mismatch" when BOTH names carry an identifiable, different saint, so
    # a plain (non-saint) nombre is never caught here. Relegate rather than offer
    # a bogus candidate.
    if saint_status == "saint_mismatch":
        return "relegated"

    # --- CANDIDATE ---
    if fuzzy_score >= 0.85:
        return "candidate"

    # --- RELEGATED ---
    return "relegated"
