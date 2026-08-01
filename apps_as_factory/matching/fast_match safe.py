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
                # Keep all tokens >= 2 chars for now
                # We'll be more strict during matching if only short tokens match
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
    Strict mode: if an input field is present at a given level but does NOT match
    the gazetteer at that level, stop immediately.

    Returns (level, match_type, had_mismatch) where:
      - level/match_type are empty strings on no-match
      - had_mismatch=True  → genuine conflict: both sides had a value but disagreed
      - had_mismatch=False → either a match was found, or gazetteer had no value at
                             that level (absence, not contradiction)
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
        ref_in_val = None
        for col in possible_fields:
            if col in row.index and pd.notna(row[col]) and str(row[col]).strip():
                ref_in_val = str(row[col]).strip().lower()
                break
        if not ref_in_val:
            continue  # level not available in input — skip

        ref_cols = [c for c in ref_row.index if level in c and not c.startswith("ref_")]
        ref_vals = [str(ref_row[c]).strip().lower() for c in ref_cols if pd.notna(ref_row[c]) and str(ref_row[c]).strip()]

        if not ref_vals:
            # Gazetteer has no value at this level — absence, not mismatch; stop
            return ("", "", False)

        for val in ref_vals:
            if ref_in_val == val:
                return (level, "exact", False)
            if fuzz.partial_ratio(ref_in_val, val) >= 85:
                return (level, "fuzzy", False)

        # Both sides had a value but they disagreed — genuine mismatch; stop
        return ("", "", True)

    return ("", "", False)


def match_batch(input_df, ref_df, ref_index, saints_list, start=0, end=None, logger=None, debug_logger=None, target_labels=None):
    """Match a batch of input rows against the reference gazetteer."""
    results = []
    subset = input_df.iloc[start:end] if end else input_df
    
    if target_labels is None:
        target_labels = []
    target_labels = [str(t).lower().strip() for t in target_labels]

    for idx, row in subset.iterrows():
        try:
            label = str(row.get("ref_Label", "")).strip()
            row_id = row.get("rowID")
            
            label_cleaned = clean_toponym(label)
            is_target = label.lower() in target_labels
            
            if logger and idx % 10 == 0:
                logger.info(f"Processing row {idx}, rowID={row_id}")
        
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
                    "lugar_provincia_generica": "",
                    "fine_level_mismatch": False,
                })
                continue

            candidates = get_candidates(
                label_cleaned,
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
                    "lugar_provincia_generica": "",
                    "fine_level_mismatch": False,
                })
                continue

            if is_target and debug_logger:
                debug_logger.info(f"\n✅ Found {len(candidates)} token-based candidates")

            # Check token match quality - filter out weak matches based only on short tokens
            input_tokens = set(label_cleaned.split())
            long_tokens = [t for t in input_tokens if len(t) >= 3]
            
            if is_target and debug_logger:
                debug_logger.info(f"  Input tokens: {input_tokens}")
                debug_logger.info(f"  Long tokens (≥3): {long_tokens}")
            
            if long_tokens and len(input_tokens) > 1:  # Only filter if multi-word input
                # Require at least 2 matching tokens OR 1 long substantive token (not common words)
                # Common words that don't count as substantive: santa, san, santo, nuestra, senora
                common_words = {'santa', 'san', 'santo', 'nuestra', 'senora', 'maria'}
                substantive_long_tokens = [t for t in long_tokens if t not in common_words]
                
                if logger:
                    logger.info(f"  Filtering: need match on substantive tokens: {substantive_long_tokens} OR 2+ total tokens")
                
                if is_target and debug_logger:
                    debug_logger.info(f"  Substantive long tokens (non-common): {substantive_long_tokens}")
                
                strong_candidates = []
                weak_filtered = []
                for idx, cand in candidates.iterrows():
                    cand_label = clean_toponym(str(cand.get('lugar_label', '')))
                    cand_nombre = clean_toponym(str(cand.get('lugar_nombre', '')))
                    cand_variantes = clean_toponym(str(cand.get('lugar_variantes', '')))
                    
                    cand_tokens = set()
                    for val in [cand_label, cand_nombre, cand_variantes]:
                        if val:
                            cand_tokens.update(val.replace("@", " ").split())
                    
                    # Count matching tokens
                    matching_tokens = input_tokens & cand_tokens
                    matching_substantive = [t for t in substantive_long_tokens if t in cand_tokens]
                    
                    # Keep if: (1) matches substantive token, OR (2) matches 2+ tokens
                    if matching_substantive or len(matching_tokens) >= 2:
                        strong_candidates.append(idx)
                    else:
                        weak_filtered.append((cand['gz_id'], cand['lugar_label'], matching_tokens))
                
                if is_target and debug_logger:
                    debug_logger.info(f"\n  Token filtering results:")
                    debug_logger.info(f"    Kept: {len(strong_candidates)} candidates")
                    debug_logger.info(f"    Filtered: {len(weak_filtered)} candidates")
                    if weak_filtered:
                        debug_logger.info(f"    Examples of filtered:")
                        for gz_id, label, tokens in weak_filtered[:10]:
                            debug_logger.info(f"      gz_id={gz_id} '{label}': only matched {tokens}")
                
                if strong_candidates and len(strong_candidates) < len(candidates):
                    candidates = candidates.loc[strong_candidates]
                    if logger:
                        logger.info(f"  After filtering weak token matches: {len(candidates)} candidates")

            names = candidates["lugar_label"].fillna("").tolist()
            # CRITICAL FIX: Clean the candidate names before fuzzy matching
            names_cleaned = [clean_toponym(str(name)) for name in names]
            
            matches = process.extract(
                label_cleaned,
                names_cleaned,  # Compare cleaned vs cleaned
                scorer=fuzz.partial_ratio,
                limit=500,
                score_cutoff=70
            )

            if logger:
                logger.info(f"  Fuzzy matches (score >= 70, limit=500): {len(matches)}")
            
            if is_target and debug_logger:
                debug_logger.info(f"\n📊 Fuzzy matching results (scorer=partial_ratio, limit=500, cutoff=70):")
                debug_logger.info(f"  Total matches: {len(matches)}")
                if matches:
                    debug_logger.info(f"  Top 10 matches:")
                    for match_str, score, match_idx in matches[:10]:
                        ref_row = candidates.iloc[match_idx]
                        debug_logger.info(f"    Score {score}: '{match_str}' (gz_id={ref_row['gz_id']} | label='{ref_row['lugar_label']}')")
                else:
                    debug_logger.info(f"  ❌ NO MATCHES - all candidates scored below 70!")

            if not matches:
                if logger:
                    logger.warning(f"  No fuzzy matches found - checking why")
                # Continue to relegated logic below
            
            row_candidates = []
            for match_str, score, match_idx in matches:
                ref_row = candidates.iloc[match_idx]
                gz_id = int(ref_row["gz_id"])

                # PRIORITY 1: Check for full match on lugar_nombre FIRST
                nombre_match = False
                nombre_cleaned = ""
                if pd.notna(ref_row.get("lugar_nombre")):
                    nombre_cleaned = clean_toponym(str(ref_row["lugar_nombre"]))
                    if label_cleaned == nombre_cleaned:
                        nombre_match = True
                        score = 100  # Perfect score for nombre match
                
                # For saint/toponym comparison:
                # If nombre matches, compare against nombre; otherwise against label
                saint_status = "no_saint"
                toponym_type = "none"
                score_val = 0.0
                
                if nombre_match:
                    # Check saint in BOTH the input and the nombre
                    saint_in_input = extract_saint(label_cleaned, saints_list)
                    saint_in_nombre = extract_saint(nombre_cleaned, saints_list)
                    
                    if is_target and debug_logger:
                        debug_logger.info(f"     NOMBRE MATCH detected")
                        debug_logger.info(f"     Saint in input '{label_cleaned}': {saint_in_input}")
                        debug_logger.info(f"     Saint in nombre '{nombre_cleaned}': {saint_in_nombre}")
                    
                    if saint_in_input and saint_in_nombre:
                        if saint_in_input == saint_in_nombre:
                            saint_status = "saint_match"
                        else:
                            saint_status = "saint_mismatch"
                    elif saint_in_input and not saint_in_nombre:
                        # Input has saint, nombre doesn't - check if nombre contains the input
                        # e.g., "Pasto" (input) vs "San Juan de Pasto" (nombre)
                        input_without_saint = label_cleaned.replace(saint_in_input, "").strip() if saint_in_input else label_cleaned
                        if input_without_saint in nombre_cleaned:
                            saint_status = "saint_match"  # Input is subset of nombre
                        else:
                            saint_status = "saint_mismatch"
                    elif not saint_in_input and saint_in_nombre:
                        # Nombre has saint, input doesn't - check if input is contained in nombre
                        # e.g., "Pasto" (input) vs "San Juan de Pasto" (nombre)  
                        if label_cleaned in nombre_cleaned:
                            saint_status = "no_saint"  # Input is just the core name, no saint
                        else:
                            saint_status = "saint_mismatch"
                    else:
                        # Neither has saint
                        saint_status = "no_saint"
                    
                    toponym_type = "toponym_nombre"  # Full match on nombre
                    score_val = 1.0
                else:
                    # Regular comparison against label/nombre/variantes
                    saint_status, toponym_type, score_val = compare_toponyms(label_cleaned, ref_row, saints_list)
                
                cat_status = compare_category(
                    row.get("ref_categoria"),
                    ref_row.get("lugar_categoria"),
                    ref_row.get("lugar_categoria_especial")
                )
                score = max(score, score_val * 100)

                level, match_type, had_mismatch = territory_match(row, ref_row)
                territory_value = f"{level}_{match_type}" if level else ""
                phase = decide_phase_outcome(toponym_type, saint_status, cat_status, score / 100)

                # Determine if this is a saint-only match (no substantive toponym beyond the saint)
                is_saint_only = False
                saint_in_label = extract_saint(label_cleaned, saints_list)

                if saint_status == "saint_match" and not nombre_match:
                    if saint_in_label:
                        label_without_saint = label_cleaned.replace(saint_in_label, "").strip()
                        if not label_without_saint or len(label_without_saint) < 3:
                            is_saint_only = True

                # Saint-only matches must always require manual confirmation
                if is_saint_only and phase == "auto_adopt":
                    phase = "candidate"

                if is_target and debug_logger:
                    debug_logger.info(f"\n  🔍 Saint detection for '{label_cleaned}':")
                    debug_logger.info(f"     Extracted saint from input: {saint_in_label}")
                    if nombre_match:
                        debug_logger.info(f"     NOMBRE MATCH: '{nombre_cleaned}'")
                        debug_logger.info(f"     Extracted saint from nombre: {extract_saint(nombre_cleaned, saints_list)}")
                    debug_logger.info(f"     Label without saint: '{label_cleaned.replace(saint_in_label, '').strip() if saint_in_label else ''}'")
                    debug_logger.info(f"     Is saint-only: {is_saint_only}")

                if is_target and debug_logger:
                    debug_logger.info(f"\n  Candidate gz_id={gz_id} '{match_str}':")
                    debug_logger.info(f"    Fuzzy score: {score/100:.3f}")
                    debug_logger.info(f"    Nombre match: {nombre_match}")
                    debug_logger.info(f"    Saint-only match: {is_saint_only}")
                    debug_logger.info(f"    Saint status: {saint_status}")
                    debug_logger.info(f"    Toponym type: {toponym_type}")
                    debug_logger.info(f"    Category status: {cat_status}")
                    debug_logger.info(f"    Territory match: {territory_value}")
                    debug_logger.info(f"    Phase outcome: {phase}")

                if logger:
                    logger.debug(f"    gz_id={gz_id} '{match_str}' score={score/100:.3f} nombre={nombre_match} saint_only={is_saint_only} terr={territory_value} phase={phase}")

                row_candidates.append({
                    "rowID": row_id,
                    "ref_Label": label,
                    "gz_id": gz_id,
                    "fuzzy_score": score,
                    "category_match": cat_status,
                    "saint_match": saint_status,
                    "territories_match": territory_value,
                    "phase_1a_outcome": phase,
                    "toponym_match": toponym_type,
                    "lugar_label": ref_row.get("lugar_label", ""),
                    "lugar_partido_generico": ref_row.get("lugar_partido_generico", ""),
                    "lugar_provincia_generica": ref_row.get("lugar_provincia_generica", ""),
                    "nombre_match": nombre_match,
                    "saint_only": is_saint_only,
                    "had_mismatch": had_mismatch,
                })

            # Aggregate mismatch flag across all candidates before any filtering
            any_mismatch = any(c.get("had_mismatch", False) for c in row_candidates)

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
                    "lugar_provincia_generica": "",
                    "fine_level_mismatch": False,
                })
                continue

            if logger:
                logger.info(f"  Before dedup: {len(row_candidates)} candidates")

            unique_candidates = {}
            for cand in row_candidates:
                gz = cand["gz_id"]
                if gz not in unique_candidates:
                    unique_candidates[gz] = cand
            row_candidates = list(unique_candidates.values())
            
            if logger:
                logger.info(f"  After dedup: {len(row_candidates)} unique gz_ids")

            # Hierarchical filtering: prioritize nombre matches and non-saint-only matches
            nombre_matches = [r for r in row_candidates if r.get("nombre_match", False)]
            saint_only_matches = [r for r in row_candidates if r.get("saint_only", False)]
            substantive_matches = [r for r in row_candidates if not r.get("saint_only", False)]
            
            if logger:
                logger.info(f"  Nombre matches (exact on lugar_nombre): {len(nombre_matches)}")
                logger.info(f"  Saint-only matches: {len(saint_only_matches)}")
                logger.info(f"  Substantive matches: {len(substantive_matches)}")
            
            # Priority 1: If we have nombre matches, ONLY keep those
            if nombre_matches:
                row_candidates = nombre_matches
                if logger:
                    logger.info(f"  ✅ NOMBRE PRIORITY: Keeping ONLY {len(row_candidates)} nombre matches, discarding {len(substantive_matches) - len(nombre_matches)} others")
                    for r in row_candidates:
                        logger.info(f"     gz_id={r['gz_id']}: {r['lugar_label']}")
            # Priority 2: If we have substantive matches but NO nombre matches, discard saint-only
            elif substantive_matches:
                row_candidates = substantive_matches
                if logger and saint_only_matches:
                    logger.info(f"  Discarding {len(saint_only_matches)} saint-only matches, keeping {len(row_candidates)} substantive matches")

            has_territory_match = [r for r in row_candidates if r["territories_match"]]
            
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
            else:
                if logger:
                    if has_input_territory:
                        logger.info(f"  No territorial filtering applied (no matching territories found)")
                    else:
                        logger.info(f"  No territorial filtering applied (no input territorial data)")

            # Auto-adopt logic — saint-only matches are always excluded
            auto_adopts = []
            for r in row_candidates:
                saint_ok = r["saint_match"] in ("saint_match", "no_saint")
                score_high = r["fuzzy_score"] > 90
                has_territory = bool(r["territories_match"])
                not_saint_only = not r.get("saint_only", False)

                if saint_ok and score_high and has_territory and not_saint_only:
                    auto_adopts.append(r)

            if logger:
                logger.info(f"  Auto-adopt candidates: {len(auto_adopts)}")

            if len(auto_adopts) == 1:
                auto_adopts[0]["phase_1a_outcome"] = "auto_adopt"
                # Remove tracking fields before adding to results
                auto_adopts[0].pop("nombre_match", None)
                auto_adopts[0].pop("saint_only", None)
                auto_adopts[0].pop("had_mismatch", None)
                if logger:
                    logger.info(f"  Single auto-adopt, discarding other matches")
                results.append(auto_adopts[0])
            elif len(auto_adopts) > 1:
                for r in auto_adopts:
                    r["phase_1a_outcome"] = "candidate"
                    # Remove tracking fields
                    r.pop("nombre_match", None)
                    r.pop("saint_only", None)
                    r.pop("had_mismatch", None)
                if logger:
                    logger.info(f"  Multiple auto-adopts, keeping as candidates")
                results.extend(auto_adopts)
            else:
                valids = [r for r in row_candidates if r["phase_1a_outcome"] == "candidate"]
                if logger:
                    logger.info(f"  Valid candidates (phase=candidate): {len(valids)}")

                if valids:
                    # Remove tracking fields
                    for v in valids:
                        v.pop("nombre_match", None)
                        v.pop("saint_only", None)
                        v.pop("had_mismatch", None)
                    results.extend(valids)
                else:
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
                        "lugar_provincia_generica": "",
                        "fine_level_mismatch": any_mismatch,
                    })
        
        except Exception as e:
            if logger:
                logger.error(f"ERROR processing rowID={row.get('rowID')}: {e}")
                logger.error(f"Label: '{row.get('ref_Label')}'")
            results.append({
                "rowID": row.get("rowID"),
                "ref_Label": str(row.get("ref_Label", "")),
                "gz_id": None,
                "fuzzy_score": None,
                "category_match": "error",
                "saint_match": "error",
                "territories_match": "error",
                "phase_1a_outcome": "relegated",
                "toponym_match": "none",
                "lugar_label": "",
                "lugar_partido_generico": "",
                "lugar_provincia_generica": ""
            })

    return results
