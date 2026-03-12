# main/matching/fast_match.py
import pandas as pd
from rapidfuzz import process, fuzz
from .score import decide_phase_outcome
from .filters import compare_category
from .saints import extract_saint, compare_saints
from .cleaning import clean_toponym
from .toponym_match import compare_toponyms


def build_ref_index(ref_df: pd.DataFrame, logger=None) -> dict:
    """Create token -> row_id mapping for quick pre-filter."""
    index = {}
    short_tokens_skipped = 0
    
    for i, row in ref_df.iterrows():
        for col in ["lugar_label", "lugar_nombre", "lugar_variantes"]:
            val = clean_toponym(str(row.get(col, "")))
            if not val:
                continue
            for token in val.replace("@", " ").split():
                # FIXED: Lower threshold from 3 to 2 to catch short place names
                # Still filter out single-char tokens which are usually noise
                if len(token) < 2:
                    short_tokens_skipped += 1
                    continue
                index.setdefault(token, set()).add(i)
    
    if logger:
        logger.info(f"Index built: {len(index)} unique tokens, {short_tokens_skipped} single-char tokens skipped")
    
    return index


def get_candidates(clean_label: str, ref_df: pd.DataFrame, ref_index: dict, debug_logger=None, label=None):
    """Select subset of reference rows sharing at least one token with label."""
    tokens = set(clean_label.split())
    
    if debug_logger:
        debug_logger.info(f"  Cleaned label: '{clean_label}'")
        debug_logger.info(f"  Tokens extracted: {tokens}")
        debug_logger.info(f"  Token lengths: {[len(t) for t in tokens]}")
    
    candidate_ids = set()
    for tok in tokens:
        matches = ref_index.get(tok, set())
        if debug_logger:
            if matches:
                debug_logger.info(f"    Token '{tok}' (len={len(tok)}) -> {len(matches)} candidate rows")
            else:
                debug_logger.info(f"    Token '{tok}' (len={len(tok)}) -> NOT FOUND in index")
        candidate_ids |= matches
    
    if debug_logger:
        debug_logger.info(f"  Total unique candidate row indices: {len(candidate_ids)}")
    
    if not candidate_ids:
        return pd.DataFrame(columns=ref_df.columns)
    
    candidates = ref_df.loc[list(candidate_ids)]
    
    if debug_logger:
        debug_logger.info(f"  Candidate lugares:")
        for idx, row in candidates.iterrows():
            debug_logger.info(f"    gz_id={row['gz_id']}: '{row.get('lugar_label', '')}' | nombre='{row.get('lugar_nombre', '')}' | variantes='{row.get('lugar_variantes', '')}'")
    
    return candidates

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


def match_batch(input_df, ref_df, ref_index, saints_list, start=0, end=None, logger=None, debug_logger=None, target_labels=None):
    """
    Match a batch of input rows against the reference gazetteer.
    
    Args:
        debug_logger: Optional logger for deep debugging specific cases
        target_labels: List of lowercase ref_labels to debug in detail
    """
    results = []
    subset = input_df.iloc[start:end] if end else input_df
    
    if target_labels is None:
        target_labels = []
    target_labels = [str(t).lower().strip() for t in target_labels]

    for idx, row in subset.iterrows():
        label = str(row.get("ref_Label", "")).strip()  # DON'T lowercase yet
        row_id = row.get("rowID")
        
        # Clean the label for matching
        label_cleaned = clean_toponym(label)
        
        # Check if this is a target label for deep debug
        is_target = label.lower() in target_labels
        
        if is_target and debug_logger:
            debug_logger.info(f"\n{'='*80}")
            debug_logger.info(f"🔍 DEEP DEBUG: rowID={row_id} | ref_Label='{label}'")
            debug_logger.info(f"  Cleaned to: '{label_cleaned}'")
            debug_logger.info(f"{'='*80}")
            debug_logger.info(f"Input row data:")
            for col in row.index:
                if col.startswith('ref_') or col == 'rowID':
                    debug_logger.info(f"  {col}: {row[col]}")
        
        if logger:
            logger.info(f"\n{'='*60}")
            logger.info(f"Processing rowID={row_id}: '{label}' -> cleaned: '{label_cleaned}'")
        
        if not label_cleaned:
            if logger:
                logger.warning(f"  Empty label after cleaning - relegated")
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

        # Get candidates with deep debug for target labels
        candidates = get_candidates(
            label_cleaned,  # FIXED: Pass cleaned label, not raw
            ref_df, 
            ref_index, 
            debug_logger=debug_logger if is_target else None,
            label=label
        )
        
        if logger:
            logger.info(f"  Token-based candidates: {len(candidates)}")
        
        if candidates.empty:
            if logger:
                logger.warning(f"  No candidates found - relegated")
            if is_target and debug_logger:
                debug_logger.info(f"\n❌ PROBLEM: No candidates found via token index!")
                debug_logger.info(f"   This means no tokens from the cleaned label matched the index.")
                debug_logger.info(f"   Checking if gz_id should exist in reference...")
            results.append({
                "rowID": row_id,
                "ref_Label": label,  # Store original label
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

        if is_target and debug_logger:
            debug_logger.info(f"\n✅ Found {len(candidates)} token-based candidates")

        names = candidates["lugar_label"].fillna("").tolist()
        matches = process.extract(
            label_cleaned,  # FIXED: Use cleaned label for fuzzy matching too
            names,
            scorer=fuzz.partial_ratio,
            limit=500,
            score_cutoff=70
        )

        if logger:
            logger.info(f"  Fuzzy matches (score >= 70, limit=20): {len(matches)}")
        
        if is_target and debug_logger:
            debug_logger.info(f"\n📊 Fuzzy matching results (scorer=partial_ratio, limit=20, cutoff=70):")
            if matches:
                for match_str, score, match_idx in matches:
                    ref_row = candidates.iloc[match_idx]
                    debug_logger.info(f"  Score {score}: '{match_str}' (gz_id={ref_row['gz_id']})")
            else:
                debug_logger.info(f"  ❌ NO MATCHES - all candidates scored below 70!")
                debug_logger.info(f"  Checking scores for all candidates:")
                for idx, cand_row in candidates.iterrows():
                    cand_label = cand_row['lugar_label']
                    score = fuzz.partial_ratio(label, cand_label)
                    debug_logger.info(f"    Score {score}: '{cand_label}' (gz_id={cand_row['gz_id']})")

        row_candidates = []
        for match_str, score, match_idx in matches:
            ref_row = candidates.iloc[match_idx]
            gz_id = int(ref_row["gz_id"])

            saint_status, toponym_type, score_val = compare_toponyms(label_cleaned, ref_row, saints_list)  # FIXED
            cat_status = compare_category(
                row.get("ref_categoria"),
                ref_row.get("lugar_categoria"),
                ref_row.get("lugar_categoria_especial")
            )
            score = max(score, score_val * 100)

            level, match_type = territory_match(row, ref_row)
            territory_value = f"{level}_{match_type}" if level else ""
            phase = decide_phase_outcome(toponym_type, saint_status, cat_status, score / 100)

            if is_target and debug_logger:
                debug_logger.info(f"\n  Candidate gz_id={gz_id} '{match_str}':")
                debug_logger.info(f"    Fuzzy score: {score/100:.3f}")
                debug_logger.info(f"    Saint status: {saint_status}")
                debug_logger.info(f"    Toponym type: {toponym_type}")
                debug_logger.info(f"    Category status: {cat_status}")
                debug_logger.info(f"    Territory match: {territory_value}")
                debug_logger.info(f"    Phase outcome: {phase}")

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

        if not row_candidates:
            if logger:
                logger.warning(f"  No valid candidates after scoring - relegated")
            if is_target and debug_logger:
                debug_logger.info(f"\n❌ No candidates passed fuzzy matching threshold")
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

        # remove duplicate gz_id
        unique_candidates = {}
        for cand in row_candidates:
            gz = cand["gz_id"]
            if gz not in unique_candidates:
                unique_candidates[gz] = cand
        row_candidates = list(unique_candidates.values())
        
        if logger:
            logger.info(f"  After dedup: {len(row_candidates)} unique gz_ids")

        # Territorial filtering
        has_territory_match = [r for r in row_candidates if r["territories_match"]]
        
        # Check if input row has ANY territorial information to filter by
        has_input_territory = any([
            pd.notna(row.get(col)) and str(row.get(col)).strip()
            for col in row.index
            if col.startswith('ref_') and col not in ['ref_Label', 'ref_START', 'ref_END', 'ref_categoria']
        ])
        
        if logger:
            logger.info(f"  Input has territorial data: {has_input_territory}")
            logger.info(f"  Candidates with territory match: {len(has_territory_match)}")
        
        if is_target and debug_logger:
            debug_logger.info(f"\n🗺️  Territory filtering:")
            debug_logger.info(f"  Input has territorial data: {has_input_territory}")
            debug_logger.info(f"  Candidates with territory match: {len(has_territory_match)}")
            if has_territory_match:
                for r in has_territory_match:
                    debug_logger.info(f"    gz_id={r['gz_id']} terr={r['territories_match']}")
            else:
                debug_logger.info(f"    (none have territory matches)")
        
        # Only apply territorial filtering if:
        # 1. Input row has territorial data, AND
        # 2. At least one candidate has a territory match
        if has_input_territory and has_territory_match:
            level_order = [
                "partido", "jurisdiccion", "provincia_menor", "provincia",
                "provincia_mayor", "obispado", "adm1", "audiencia",
                "adm0", "adm0_iso", "region"
            ]

            def level_index(r):
                lvl = r["territories_match"].split("_")[0] if r["territories_match"] else "zzz"
                return level_order.index(lvl) if lvl in level_order else len(level_order)

            min_level_idx = min(level_index(r) for r in has_territory_match)
            row_candidates = [r for r in has_territory_match if level_index(r) == min_level_idx]
            
            if logger:
                min_level = level_order[min_level_idx] if min_level_idx < len(level_order) else "unknown"
                logger.info(f"  After territorial filter (keeping {min_level}): {len(row_candidates)} candidates")
            
            if is_target and debug_logger:
                min_level = level_order[min_level_idx] if min_level_idx < len(level_order) else "unknown"
                debug_logger.info(f"  Applied filter: kept only smallest level '{min_level}': {len(row_candidates)} candidates")
        else:
            # No territorial filtering - keep all candidates
            if logger and has_input_territory:
                logger.info(f"  No territorial filtering applied (no matching territories found)")
            elif logger:
                logger.info(f"  No territorial filtering applied (no input territorial data)")
            
            if is_target and debug_logger:
                if has_input_territory:
                    debug_logger.info(f"  No territorial filtering applied: input has territory but no candidates matched")
                else:
                    debug_logger.info(f"  No territorial filtering applied: input has no territorial data")

        # Auto-adopt logic
        auto_adopts = [r for r in row_candidates if r["phase_1a_outcome"] == "auto_adopt"]

        if logger:
            logger.info(f"  Auto-adopt candidates: {len(auto_adopts)}")

        if auto_adopts:
            for r in auto_adopts:
                r["phase_1a_outcome"] = "candidate"
            if logger:
                logger.info(f"  Downgraded {len(auto_adopts)} auto_adopt to candidate, discarding other matches")
            if is_target and debug_logger:
                debug_logger.info(f"\n✅ Found {len(auto_adopts)} auto-adopt matches (downgraded to candidate)")
            results.extend(auto_adopts)
        else:
            valids = [
                r for r in row_candidates
                if r["phase_1a_outcome"] == "candidate"
            ]
            if logger:
                logger.info(f"  Valid candidates (phase=candidate): {len(valids)}")
            
            if is_target and debug_logger:
                debug_logger.info(f"\n📋 Final valid candidates: {len(valids)}")
                for v in valids:
                    debug_logger.info(f"  gz_id={v['gz_id']}: {v['lugar_label']}")
                
            if valids:
                results.extend(valids)
            else:
                if logger:
                    logger.warning(f"  No valid candidates after phase filtering - relegated")
                if is_target and debug_logger:
                    debug_logger.info(f"\n❌ RELEGATED: No candidates with phase=candidate")
                    debug_logger.info(f"   All candidates were filtered out or had wrong phase")
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
