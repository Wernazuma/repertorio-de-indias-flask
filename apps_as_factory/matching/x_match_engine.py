# main/matching/match_engine.py
import pandas as pd
from fuzzywuzzy import fuzz
from .cleaning import clean_toponym
from .saints import extract_saint, compare_saints
from .filters import prefilter_by_territory, compare_category
from .score import decide_phase_outcome

def match_row(row, ref_df, saints_list):
    """Match one input row to potential reference gazetteer entries."""
    ref_label = str(row.get("ref_Label", "")).strip()
    if not ref_label:
        return []

    cleaned_label = clean_toponym(ref_label)
    saint_in_label = extract_saint(ref_label, saints_list)
    filtered_df = prefilter_by_territory(ref_df, row)

    results = []

    for _, ref_row in filtered_df.iterrows():
        for field in ["lugar_label", "lugar_nombre", "lugar_variantes"]:
            val = ref_row.get(field)
            if pd.isna(val) or not str(val).strip():
                continue

            # handle @-separated variants
            variants = [clean_toponym(v) for v in str(val).split("@")]

            for cand in variants:
                if not cand:
                    continue

                score = fuzz.partial_ratio(cleaned_label, cand) / 100
                if score < 0.85:
                    continue

                saint_in_ref = extract_saint(cand, saints_list)
                saint_status = compare_saints(saint_in_label, saint_in_ref)

                category_status = compare_category(
                    row.get("ref_categoria", None),
                    ref_row.get("lugar_categoria", None),
                    ref_row.get("lugar_categoria_especial", None)
                )

                phase = decide_phase_outcome(
                    field, saint_status, category_status, score
                )

                results.append({
                    "rowID": row.get("rowID"),
                    "ref_Label": ref_label,
                    "gz_id": int(ref_row.get("gz_id", -1)),
                    "lugar_nombre": ref_row.get("lugar_nombre", ""),
                    "lugar_categoria": ref_row.get("lugar_categoria", ""),
                    "fuzzy_score": round(score, 3),
                    "saint_status": saint_status,
                    "category_status": category_status,
                    "phase_1_outcome": phase,
                    "match_field": field
                })
    return results


def run_matching(input_df, ref_df, saints_list, status_callback=None):
    """Run fuzzy + rule-based matching for all input rows."""
    results = []
    total = len(input_df)

    for i, (_, row) in enumerate(input_df.iterrows(), start=1):
        row_results = match_row(row, ref_df, saints_list)
        results.extend(row_results)

        if status_callback and i % 50 == 0:
            status_callback(i, total)

    return pd.DataFrame(results)
