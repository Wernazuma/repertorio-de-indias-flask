# main/matching/fast_match.py
import pandas as pd
from rapidfuzz import process, fuzz
from .score import decide_phase_outcome
from .filters import compare_category
from .saints import extract_saint, compare_saints
from .cleaning import clean_toponym
from .toponym_match import compare_toponyms


def build_ref_index(ref_df: pd.DataFrame) -> dict:
    """Create token -> row_id mapping for quick pre-filter."""
    index = {}
    for i, row in ref_df.iterrows():
        for col in ["lugar_label", "lugar_nombre", "lugar_variantes"]:
            val = clean_toponym(str(row.get(col, "")))
            if not val:
                continue
            for token in val.replace("@", " ").split():
                if len(token) < 3:
                    continue
                index.setdefault(token, set()).add(i)
    return index


def get_candidates(clean_label: str, ref_df: pd.DataFrame, ref_index: dict) -> pd.DataFrame:
    """Select subset of reference rows sharing at least one token with label."""
    tokens = set(clean_label.split())
    candidate_ids = set()
    for tok in tokens:
        candidate_ids |= ref_index.get(tok, set())
    if not candidate_ids:
        return pd.DataFrame(columns=ref_df.columns)
    return ref_df.loc[list(candidate_ids)]

def territory_match(row, ref_row):
    """
    Compare territorial levels from small to large.
    Return (best_level, match_type) or ("", "") if no match.
    """
    levels = [
        ("partido", ["ref_Partido", "ref_partido"]),
        ("jurisdiccion", ["ref_Jurisdiccion"]),
        ("provincia_menor", ["ref_ProvinciaMenor"]),
        ("provincia", ["ref_Provincia"]),
        ("provincia_mayor", ["ref_ProvinciaMayor"]),
        ("obispado", ["ref_Obispado"]),
        ("adm1", ["ref_Adm1"]),
        ("audiencia", ["ref_Audiencia"]),
        ("adm0", ["ref_Adm0", "ref_Adm0_Label"]),
        ("adm0_iso", ["ref_Adm0_ISO"]),
        ("region", ["ref_Region"]),
    ]

    for level, possible_fields in levels:
        # find an input ref_ column that exists and is non-empty
        ref_in_val = None
        for col in possible_fields:
            if col in row.index and pd.notna(row[col]) and str(row[col]).strip():
                ref_in_val = str(row[col]).strip().lower()
                break
        if not ref_in_val:
            continue  # this level not available in input

        # get reference fields for same level
        ref_cols = [c for c in ref_row.index if level in c and not c.startswith("ref_")]
        ref_vals = [str(ref_row[c]).strip().lower() for c in ref_cols if pd.notna(ref_row[c]) and str(ref_row[c]).strip()]

        if not ref_vals:
            continue  # this level not in reference record

        # compare input val to all ref vals
        for val in ref_vals:
            if ref_in_val == val:
                return (level, "exact")
            if fuzz.partial_ratio(ref_in_val, val) >= 85:
                return (level, "fuzzy")

    return ("", "")


def match_batch(input_df, ref_df, ref_index, saints_list, start=0, end=None, logger=None):
    results = []
    subset = input_df.iloc[start:end] if end else input_df

    for idx, row in subset.iterrows():
        label = str(row.get("ref_Label", "")).strip().lower()
        row_id = row.get("rowID")
        adm0_iso = row.get("ref_Adm0_ISO", "")
        
        if logger:
            logger.info(f"\n{'='*60}")
            logger.info(f"Processing rowID={row_id}: '{label}' | Adm0_ISO={adm0_iso}")
        
        if not label:
            # empty label → relegated
            if logger:
                logger.warning(f"  Empty label - relegated")
            results.append({
                "rowID": row_id,
                "ref_Label": "",
                "gz_id": None,
                "fuzzy_score": None,
                "category_match": "pending",
                "saint_match": "pending",
                "territories_match": "unknown",
                "phase_1a_outcome": "relegated",
                "toponym_match": "none",
                "lugar_label": "",
                "lugar_partido_generico": "",
                "lugar_provincia_generica": ""
            })
            continue

        candidates = get_candidates(label, ref_df, ref_index)
        
        if logger:
            logger.info(f"  Token-based candidates: {len(candidates)}")
            if len(candidates) > 0:
                santa_cruz_candidates = candidates[candidates['lugar_label'].str.contains('Santa Cruz', case=False, na=False)]
                tenerife_candidates = candidates[candidates['lugar_label'].str.contains('Tenerife', case=False, na=False)]
                logger.info(f"    - 'Santa Cruz' matches: {len(santa_cruz_candidates)}")
                logger.info(f"    - 'Tenerife' matches: {len(tenerife_candidates)}")
        
        if candidates.empty:
            # no matches at all → relegated
            if logger:
                logger.warning(f"  No candidates found - relegated")
            results.append({
                "rowID": row_id,
                "ref_Label": label,
                "gz_id": None,
                "fuzzy_score": None,
                "category_match": "pending",
                "saint_match": "pending",
                "territories_match": "unknown",
                "phase_1a_outcome": "relegated",
                "toponym_match": "none",
                "lugar_label": "",
                "lugar_partido_generico": "",
                "lugar_provincia_generica": ""
            })
            continue

        names = candidates["lugar_label"].fillna("").tolist()
        matches = process.extract(
            label,
            names,
            scorer=fuzz.partial_ratio,
            limit=500,
            score_cutoff=70
        )

        if logger:
            logger.info(f"  Fuzzy matches (score >= 70): {len(matches)}")

        row_candidates = []
        for match_str, score, match_idx in matches:
            ref_row = candidates.iloc[match_idx]
            gz_id = int(ref_row["gz_id"])

            saint_status, toponym_type, score_val = compare_toponyms(label, ref_row, saints_list)
            cat_status = compare_category(
                row.get("ref_categoria"),
                ref_row.get("lugar_categoria"),
                ref_row.get("lugar_categoria_especial")
            )
            score = max(score, score_val * 100)  # blend fuzzywuzzy and toponym score

            level, match_type = territory_match(row, ref_row)
            territory_value = f"{level}_{match_type}" if level else ""
            phase = decide_phase_outcome(toponym_type, saint_status, cat_status, score / 100)

            if logger:
                logger.debug(f"    gz_id={gz_id} '{match_str}' score={score/100:.3f} saint={saint_status} cat={cat_status} terr={territory_value} phase={phase}")

            row_candidates.append({
                "rowID": row_id,
                "ref_Label": label,
                "gz_id": gz_id,
                "fuzzy_score": round(score / 100, 3),
                "category_match": cat_status,
                "saint_match": saint_status,
                "territories_match": territory_value,
                "phase_1a_outcome": phase,
                "toponym_match": toponym_type,
                "lugar_label": ref_row.get("lugar_label", ""),
                "lugar_partido_generico": ref_row.get("lugar_partido_generico", ""),
                "lugar_provincia_generica": ref_row.get("lugar_provincia_generica", "")
            })

        # --- Clean up per-row results ---

        if not row_candidates:
            if logger:
                logger.warning(f"  No valid candidates after scoring - relegated")
            results.append({
                "rowID": row_id,
                "ref_Label": label,
                "gz_id": None,
                "fuzzy_score": None,
                "category_match": "pending",
                "saint_match": "pending",
                "territories_match": "unknown",
                "phase_1a_outcome": "relegated",
                "toponym_match": "none",
                "lugar_label": "",
                "lugar_partido_generico": "",
                "lugar_provincia_generica": ""
            })
            continue

        if logger:
            logger.info(f"  Before dedup: {len(row_candidates)} candidates")

        # remove duplicate gz_id for this row
        unique_candidates = {}
        for cand in row_candidates:
            gz = cand["gz_id"]
            if gz not in unique_candidates:
                unique_candidates[gz] = cand
        row_candidates = list(unique_candidates.values())
        
        if logger:
            logger.info(f"  After dedup: {len(row_candidates)} unique gz_ids")

        # --- Territorial filtering (smallest, most specific match wins) ---
        has_territory_match = [r for r in row_candidates if r["territories_match"]]
        
        if logger:
            logger.info(f"  Candidates with territory match: {len(has_territory_match)}")
            if has_territory_match:
                for r in has_territory_match:
                    logger.debug(f"    gz_id={r['gz_id']} terr={r['territories_match']}")
        
        if has_territory_match:
            level_order = [
                "partido", "jurisdiccion", "provincia_menor", "provincia",
                "provincia_mayor", "obispado", "adm1", "audiencia",
                "adm0", "adm0_iso", "region"
            ]

            def level_index(r):
                lvl = r["territories_match"].split("_")[0] if r["territories_match"] else "zzz"
                return level_order.index(lvl) if lvl in level_order else len(level_order)

            min_level_idx = min(level_index(r) for r in has_territory_match)
            # keep only those with the same smallest-level match
            row_candidates = [r for r in has_territory_match if level_index(r) == min_level_idx]
            
            if logger:
                min_level = level_order[min_level_idx] if min_level_idx < len(level_order) else "unknown"
                logger.info(f"  After territorial filter (keeping {min_level}): {len(row_candidates)} candidates")

        # 🧠 Handle auto_adopt logic
        auto_adopts = [r for r in row_candidates if r["phase_1a_outcome"] == "auto_adopt"]

        if logger:
            logger.info(f"  Auto-adopt candidates: {len(auto_adopts)}")

        if auto_adopts:
            # Downgrade all to candidate
            for r in auto_adopts:
                r["phase_1a_outcome"] = "candidate"
            # Keep only these (throw away all other matches)
            if logger:
                logger.info(f"  Downgraded {len(auto_adopts)} auto_adopt to candidate, discarding other matches")
            results.extend(auto_adopts)
        else:
            # Otherwise keep all candidate matches
            valids = [
                r for r in row_candidates
                if r["phase_1a_outcome"] == "candidate"
            ]
            if logger:
                logger.info(f"  Valid candidates (phase=candidate): {len(valids)}")
                
            if valids:
                results.extend(valids)
            else:
                # none valid -> relegated
                if logger:
                    logger.warning(f"  No valid candidates after phase filtering - relegated")
                results.append({
                    "rowID": row_id,
                    "ref_Label": label,
                    "gz_id": None,
                    "fuzzy_score": None,
                    "category_match": "pending",
                    "saint_match": "pending",
                    "territories_match": "unknown",
                    "phase_1a_outcome": "relegated",
                    "toponym_match": "none",
                    "lugar_label": "",
                    "lugar_partido_generico": "",
                    "lugar_provincia_generica": ""
                })

    return results
