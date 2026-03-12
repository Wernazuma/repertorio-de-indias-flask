# main/matching/toponym_match.py
import pandas as pd
from rapidfuzz import fuzz
from .cleaning import clean_toponym
from .saints import extract_saint, compare_saints


def compare_toponyms(ref_label, ref_row, saints_list):
    """
    Toponym-level comparison logic.
    Applies saint-aware matching rules and returns:
    - saint_match (str)
    - toponym_match (str: 'exact' | 'fuzzy' | 'none')
    - base_match_score (float)
    """

    if not isinstance(ref_label, str) or not ref_label.strip():
        return "no_saint", "none", 0.0

    label_clean = clean_toponym(ref_label.lower())
    saint_in = extract_saint(label_clean, saints_list)
    saint_ref = None
    saint_status = "no_saint"

    # Identify if ref_label contains a saint
    has_saint = bool(saint_in)

    # Gather reference toponym variants
    variants = []
    for c in ["lugar_label", "lugar_nombre", "lugar_variantes"]:
        if c in ref_row.index:
            val = str(ref_row[c])
            if pd.notna(val) and val.strip():
                variants.extend(val.split("@"))

    variants = [clean_toponym(v.lower()) for v in variants if v.strip()]

    # If ref_label has a saint and additional text:
    if has_saint and len(label_clean.split()) > 1:
        # compare without saint → label, with saint → nombre, without saint → variantes
        saintless_label = label_clean.replace(saint_in.lower(), "").strip()
        scores = []

        for v in variants:
            # compare both saint-inclusive and saint-less forms
            s1 = fuzz.partial_ratio(saintless_label, v)
            s2 = fuzz.partial_ratio(label_clean, v)
            scores.extend([s1, s2])

        max_score = max(scores) if scores else 0
        saint_ref = extract_saint(" ".join(variants), saints_list)
        saint_status = compare_saints(saint_in, saint_ref)

        match_type = (
            "exact" if max_score == 100
            else "fuzzy" if max_score >= 85
            else "none"
        )
        return saint_status, match_type, max_score / 100

    # If ref_label has no saint:
    elif not has_saint:
        scores = [fuzz.partial_ratio(label_clean, v) for v in variants]
        max_score = max(scores) if scores else 0
        match_type = (
            "exact" if max_score == 100
            else "fuzzy" if max_score >= 85
            else "none"
        )
        return "no_saint", match_type, max_score / 100

    # If ref_label is saint only:
    else:
        saint_ref = extract_saint(" ".join(variants), saints_list)
        saint_status = compare_saints(saint_in, saint_ref)
        match_type = "exact" if saint_status == "saint_match" else "none"
        return saint_status, match_type, 1.0 if match_type == "exact" else 0.0
