# main/matching/fast_match.py
import re
import pandas as pd
from rapidfuzz import process, fuzz
from .score import decide_phase_outcome
from .filters import compare_category
from .saints import extract_saint, compare_saints
from .cleaning import clean_toponym
from .toponym_match import compare_toponyms
from .regions import region_code_to_name


# --- Relaxed prefilter (Fix A) ---
# Substantive tokens are fuzzy-expanded against the index vocabulary so that
# near-miss spellings (Bartholo/Bartolo, Colquemarca/Corquemarca, ...) still
# enter the candidate pool. The strict downstream scoring still decides the
# actual outcome, so a generous prefilter does not by itself create matches.
FUZZY_PREFILTER_THRESHOLD = 80   # fuzz.ratio cutoff on whole tokens
FUZZY_PREFILTER_MIN_LEN = 4      # only expand tokens of at least this length
COMMON_PREFILTER_TOKENS = {"santa", "san", "santo", "nuestra", "senora", "maria"}


def collapse_reference(ref_df: pd.DataFrame, logger=None) -> pd.DataFrame:
    """Collapse the per-time-slice reference to one row per place (gz_id).

    reference_gazetteer.csv carries one row per place PER time-slice (~13k places
    blown up to ~80k rows). Phase-1a matching is time-agnostic — it matches names
    and checks territorial consistency — so those duplicate slices only multiply
    the index, the candidate pools and the per-row pandas overhead (the reason a
    Tlaxcala table went from seconds to minutes when the slice count doubled).

    Identity/name columns are identical across a place's slices, so we keep the
    first. Territorial columns can differ across slices, so we keep the UNION of
    their distinct values (space-joined) — a place that sat in Tlaxcala's
    jurisdiccion during ANY slice still matches a Tlaxcala input. Chronological
    disambiguation happens later, off the full per-slice table, not here.
    """
    if "gz_id" not in ref_df.columns:
        return ref_df

    terr_cols = set()
    for level, _ in _TERRITORY_LEVELS:
        for c in ref_df.columns:
            if level in c and not c.startswith("ref_"):
                terr_cols.add(c)

    def uniq_join(series):
        seen = []
        for x in series:
            v = str(x).strip()
            if v and v.lower() != "nan" and v not in seen:
                seen.append(v)
        return " ".join(seen)

    agg = {c: (uniq_join if c in terr_cols else "first")
           for c in ref_df.columns if c != "gz_id"}
    collapsed = ref_df.groupby("gz_id", sort=False).agg(agg).reset_index()

    if logger:
        logger.info(f"Collapsed reference: {len(ref_df)} slice-rows -> "
                    f"{len(collapsed)} places")
    return collapsed


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


def get_candidates(clean_label: str, ref_df: pd.DataFrame, ref_index: dict,
                   debug_logger=None, label=None, ref_vocab=None,
                   allowed_ids=None):
    """Select subset of reference rows sharing a token with label.

    Beyond exact token equality, substantive tokens (>= FUZZY_PREFILTER_MIN_LEN
    chars, not common articles/saints) are fuzzy-expanded against the index
    vocabulary (fuzz.ratio >= FUZZY_PREFILTER_THRESHOLD) so near-miss spellings
    still reach the fuzzy scorer.

    ``allowed_ids`` (when given) restricts the candidate pool to a territorial
    context: only reference rows in that set survive. This is what keeps a
    Tlaxcala input from fuzzy-scoring every "San …" place in the Americas.
    """
    tokens = set(clean_label.split())
    if ref_vocab is None:
        ref_vocab = list(ref_index.keys())

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

        # Fuzzy expansion for substantive tokens (Fix A)
        if len(tok) >= FUZZY_PREFILTER_MIN_LEN and tok not in COMMON_PREFILTER_TOKENS:
            fuzzy_added = 0
            for cand_tok, _score, _ in process.extract(
                tok, ref_vocab, scorer=fuzz.ratio, limit=30,
                score_cutoff=FUZZY_PREFILTER_THRESHOLD,
            ):
                if cand_tok != tok:
                    extra = ref_index.get(cand_tok, set())
                    candidate_ids |= extra
                    fuzzy_added += len(extra)
            if debug_logger and fuzzy_added:
                debug_logger.info(f"    Token '{tok}' fuzzy-expanded -> +{fuzzy_added} candidate rows")

    # Territorial context gate: drop anything outside the input's territory
    # BEFORE the (expensive) fuzzy scoring downstream. Purely a pruning step —
    # the precise territory_match still runs later — so it only removes rows
    # that are territorially irrelevant.
    if allowed_ids is not None:
        candidate_ids &= allowed_ids
        if debug_logger:
            debug_logger.info(f"    After territorial context filter: {len(candidate_ids)} rows")

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

def _level_compare(ref_in_val, ref_vals):
    """Compare one input value against the gazetteer values at a single level.

    Returns "exact", "fuzzy", or None (no match). Token-boundary aware with a
    length guard so a short input doesn't match INSIDE a longer unrelated name
    (e.g. 'paria' ~ 'parinacochas' = 89).
    """
    for val in ref_vals:
        if ref_in_val == val:
            return "exact"
    in_toks = [t for t in ref_in_val.split() if len(t) >= 3]
    for val in ref_vals:
        for b in (t for t in val.split() if len(t) >= 3):
            for a in in_toks:
                if a == b:
                    return "exact"
                shorter, longer = sorted((len(a), len(b)))
                if shorter / longer >= 0.7 and fuzz.partial_ratio(a, b) >= 85:
                    return "fuzzy"
    return None


# Territorial hierarchy, fine -> coarse, mapping each level to the input columns
# that may carry it.
_TERRITORY_LEVELS = [
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


def _input_value_at_level(row, level, possible_fields):
    """Return the (lower-cased, region-mapped) input value for a level, or None
    if the input has no usable value there."""
    ref_in_val = None
    for col in possible_fields:
        if col in row.index and pd.notna(row[col]) and str(row[col]).strip():
            ref_in_val = str(row[col]).strip()
            break
    if not ref_in_val:
        return None
    # The input region is a CODE (e.g. "CHA"); translate to the gazetteer region
    # name (e.g. "Charcas"). If it can't be mapped, treat the level as absent.
    if level == "region":
        mapped = region_code_to_name(ref_in_val)
        if not mapped:
            return None
        ref_in_val = mapped
    return ref_in_val.lower()


def _gazetteer_values_at_level(ref_row, level):
    ref_cols = [c for c in ref_row.index if level in c and not c.startswith("ref_")]
    return [str(ref_row[c]).strip().lower() for c in ref_cols
            if pd.notna(ref_row[c]) and str(ref_row[c]).strip()]


# ---------------------------------------------------------------------------
# Territorial context pre-filter
#
# The reference is a per-time-slice table: ~13k places blown up to ~80k rows.
# Fuzzy-scoring every "San …" place in the Americas for a table of Tlaxcala
# parishes is what makes batches take minutes. When the input names a territory
# (e.g. Jurisdiccion = Tlaxcala) we first restrict the candidate row-ids to the
# places whose SAME level carries that value, and only fuzzy-score inside it.
#
# The level test mirrors territory_match exactly (same column selection via
# `level in c`, same "absence is not a contradiction" rule), so the surviving
# set is always a superset of what territory_match would accept — the prune only
# removes rows territory_match would veto anyway.
# ---------------------------------------------------------------------------

def _level_blob(ref_df: pd.DataFrame, level: str) -> pd.Series:
    """Lower-cased concatenation of the gazetteer columns for ONE level, cached
    on the DataFrame. Same columns _gazetteer_values_at_level reads."""
    cache = ref_df.attrs.setdefault("_level_blobs", {})
    blob = cache.get(level)
    if blob is not None and len(blob) == len(ref_df):
        return blob
    cols = [c for c in ref_df.columns if level in c and not c.startswith("ref_")]
    if cols:
        blob = (ref_df[cols].replace("nan", "").fillna("")
                .astype(str).agg(" ".join, axis=1).str.strip().str.lower())
    else:
        blob = pd.Series([""] * len(ref_df), index=ref_df.index)
    cache[level] = blob
    return blob


def context_level_value(row):
    """Return the (level, value) for the FINEST territorial field the input row
    carries (value lower-cased, region-mapped), or (None, '') when it has no
    usable territorial context. The finest level is the most discriminating."""
    for level, possible_fields in _TERRITORY_LEVELS:
        val = _input_value_at_level(row, level, possible_fields)
        if val:
            return level, val
    return None, ""


def context_allowed_ids(ref_df: pd.DataFrame, level, value):
    """Index labels of reference rows whose gazetteer value AT ``level`` matches
    ``value`` — the same exact, level-specific gate the old prefilter_by_territory
    used (input Provincia_menor/Jurisdiccion == that level in the reference).

    Rows without a value at this level are NOT kept here: a Tlaxcala-jurisdiccion
    input should only fuzzy-score places in Tlaxcala's jurisdiccion. Anything
    wrongly excluded because its gazetteer record omits the level is recovered by
    Phase 1b, which blanks the territorial fields and retries.

    Returns None (no filtering) when the value matches nothing at this level
    anywhere in the gazetteer, so a spelling the gazetteer doesn't share can't
    relegate every row — it falls back to the full search, as before.
    """
    if not level or not value:
        return None
    blob = _level_blob(ref_df, level)
    pattern = r"\b" + re.escape(value) + r"\b"
    present = blob.str.contains(pattern, regex=True, na=False)
    if not present.any():
        return None  # context unknown at this level — don't filter
    return set(ref_df.index[present])


def global_constraint_ids(ref_df: pd.DataFrame, constraints):
    """Reference rows allowed by user-set GLOBAL constraints (applied to every
    input row, e.g. "the whole table is in Charcas"). ``constraints`` is the
    parsed {prefix}_constraints.json: {"regions": [codes], "provinces": [names]}.

    Region + province are combined with AND (a province is inside its region).
    Returns a set of index labels, or None when there is nothing to constrain or
    the constraint matches nothing in the gazetteer (so it can never wipe out
    every candidate — the user just gets the unconstrained search back).
    """
    if not constraints:
        return None
    regions = [c for c in (constraints.get("regions") or []) if str(c).strip()]
    provinces = [p for p in (constraints.get("provinces") or []) if str(p).strip()]
    if not regions and not provinces:
        return None

    mask = pd.Series(True, index=ref_df.index)
    if regions and "lugar_region" in ref_df.columns:
        # lugar_region stores the 3-letter CODE (NES, PER, CHA…) — same as the
        # constraint values — so compare codes directly, no name mapping.
        rset = {str(c).strip().lower() for c in regions}
        rcol = ref_df["lugar_region"].astype(str).str.strip().str.lower()
        mask &= rcol.isin(rset)
    if provinces and "lugar_provincia_generica" in ref_df.columns:
        pset = {str(p).strip().lower() for p in provinces}
        pcol = ref_df["lugar_provincia_generica"].astype(str).str.strip().str.lower()
        mask &= pcol.isin(pset)

    ids = set(ref_df.index[mask])
    return ids if ids else None


def territory_match(row, ref_row):
    """
    Compare territorial levels from fine to coarse.

    The whole hierarchy is scanned: a mismatch at ANY level where both sides have
    a value is a contradiction (in particular a Region mismatch vetoes even when a
    finer level happens to match — Region is essentially never wrong). Absence on a
    level (gazetteer has no value there) is skipped, not treated as a contradiction.

    Returns (level, match_type, had_mismatch):
      - the finest level with a positive match (for territory-level ordering),
      - had_mismatch=True  → some level had values on both sides that disagreed,
      - had_mismatch=False → a match (or only absences) with no contradiction.
    """
    best_level, best_type = "", ""

    for level, possible_fields in _TERRITORY_LEVELS:
        ref_in_val = _input_value_at_level(row, level, possible_fields)
        if ref_in_val is None:
            continue  # level not available in input — skip

        ref_vals = _gazetteer_values_at_level(ref_row, level)
        if not ref_vals:
            continue  # gazetteer absence at this level — not a contradiction

        match_type = _level_compare(ref_in_val, ref_vals)
        if match_type is None:
            # Both sides had a value but disagreed — contradiction; veto.
            return ("", "", True)
        if not best_level:
            best_level, best_type = level, match_type
        # keep scanning coarser levels so a Region mismatch can still veto

    return (best_level, best_type, False)


def territory_detail(row, ref_row):
    """For display in disambiguation: report the FINEST level at which both the
    input and the gazetteer candidate have a value, and whether they agree.

    Returns (level_key, status) with status in 'exact' | 'fuzzy' | 'mismatch',
    or ('', '') when no level is comparable. Unlike territory_match (used for
    filtering), this surfaces a fine-level disagreement even when a coarser level
    would match — e.g. the candidate is in the right Region but NOT in the
    jurisdiccion the input names, so the user sees 'Jurisdiccion mismatch'
    instead of a green 'Region'.
    """
    for level, possible_fields in _TERRITORY_LEVELS:
        ref_in_val = _input_value_at_level(row, level, possible_fields)
        if ref_in_val is None:
            continue
        ref_vals = _gazetteer_values_at_level(ref_row, level)
        if not ref_vals:
            continue  # not comparable at this level — look coarser
        match_type = _level_compare(ref_in_val, ref_vals)
        return (level, match_type if match_type else "mismatch")
    return ("", "")


def match_batch(input_df, ref_df, ref_index, saints_list, start=0, end=None, logger=None, debug_logger=None, target_labels=None, constraints=None):
    """Match a batch of input rows against the reference gazetteer."""
    results = []
    subset = input_df.iloc[start:end] if end else input_df
    ref_vocab = list(ref_index.keys())  # for the relaxed fuzzy prefilter
    allowed_cache = {}  # context value -> allowed row-id set (per batch)
    # Global user constraints (region/province) that gate EVERY row.
    global_ids = global_constraint_ids(ref_df, constraints)
    if logger and global_ids is not None:
        logger.info(f"Global constraints active: {len(global_ids)} reference rows in scope")

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

            # Territorial context of this input row (finest level it names).
            # Memoized per batch, so a table sharing one Jurisdiccion computes
            # the allowed set exactly once.
            ctx_level, ctx_val = context_level_value(row)
            ctx_key = (ctx_level, ctx_val)
            if ctx_key not in allowed_cache:
                allowed_cache[ctx_key] = context_allowed_ids(ref_df, ctx_level, ctx_val)
            allowed_ids = allowed_cache[ctx_key]

            # Intersect the per-row context with the global constraints (both
            # must hold). Global-only when the row itself names no territory.
            if global_ids is not None:
                allowed_ids = global_ids if allowed_ids is None else (allowed_ids & global_ids)

            candidates = get_candidates(
                label_cleaned,
                ref_df,
                ref_index,
                debug_logger=debug_logger if is_target else None,
                label=label,
                ref_vocab=ref_vocab,
                allowed_ids=allowed_ids,
            )

            if logger:
                logger.info(f"  Territorial context: {ctx_level or '(none)'}='{ctx_val or ''}'"
                            f" -> allowed rows: {'all' if allowed_ids is None else len(allowed_ids)}")
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

            # Score each candidate by the BEST fuzzy over lugar_label / nombre /
            # variantes (not lugar_label alone) so that variant-only matches
            # (e.g. input 'Pilaya' -> place 'Loma' with variante 'Pilaya')
            # remain reachable. Pools are small after the token prefilter, so a
            # direct per-candidate scan is cheap.
            matches = []
            for pos in range(len(candidates)):
                cand = candidates.iloc[pos]
                cand_names = []
                for col in ("lugar_label", "lugar_nombre", "lugar_variantes"):
                    v = cand.get(col)
                    if pd.notna(v) and str(v).strip():
                        cand_names.extend(clean_toponym(str(v)).split("@"))
                cand_names = [n.strip() for n in cand_names if n.strip()]
                best = max((fuzz.partial_ratio(label_cleaned, n) for n in cand_names), default=0)
                if best >= 70:
                    label_str = clean_toponym(str(cand.get("lugar_label", "")))
                    matches.append((label_str, best, pos))
            matches.sort(key=lambda m: m[1], reverse=True)

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

            # --- Phase 1a: no territorial contradictions ---
            # A candidate that contradicts the input territory (a mismatch at the
            # finest comparable level — most importantly Region, which is
            # essentially never wrong) is NOT allowed in Phase 1a. Such candidates
            # may only resurface in a later, relaxed phase AFTER the first
            # disambiguation round. Drop them here; the aggregate mismatch flag is
            # already captured in any_mismatch for the relegation record.
            territory_consistent = [c for c in row_candidates if not c.get("had_mismatch", False)]
            if logger and len(territory_consistent) != len(row_candidates):
                logger.info(f"  Dropped {len(row_candidates) - len(territory_consistent)} territory-contradicting candidates")
            row_candidates = territory_consistent

            # --- Fix B: territory-first gating ---
            # A good hit in the correct territory must not be discarded by a
            # coincidental exact-name match elsewhere. So restrict to
            # territory-matched candidates (at the finest level) BEFORE applying
            # name priority. Only when no candidate matches the input territory
            # (or the input has no territory at all) do we fall back to the
            # previous name-priority-only behaviour — conservative, no regression.
            has_input_territory = any([
                pd.notna(row.get(col)) and str(row.get(col)).strip()
                for col in row.index
                if col.startswith('ref_') and col not in ['ref_Label', 'ref_START', 'ref_END', 'ref_categoria']
            ])
            has_territory_match = [r for r in row_candidates if r["territories_match"]]

            if logger:
                logger.info(f"  Input has territorial data: {has_input_territory}")
                logger.info(f"  Candidates with territory match: {len(has_territory_match)}")

            if is_target and debug_logger:
                debug_logger.info(f"\n🗺️  Territory filtering (territory-first):")
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

            # --- Name priority WITHIN the surviving (territory-consistent) set ---
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
                    logger.info(f"  NOMBRE PRIORITY: Keeping ONLY {len(row_candidates)} nombre matches, discarding {len(substantive_matches) - len(nombre_matches)} others")
                    for r in row_candidates:
                        logger.info(f"     gz_id={r['gz_id']}: {r['lugar_label']}")
            # Priority 2: If we have substantive matches but NO nombre matches, discard saint-only
            elif substantive_matches:
                row_candidates = substantive_matches
                if logger and saint_only_matches:
                    logger.info(f"  Discarding {len(saint_only_matches)} saint-only matches, keeping {len(row_candidates)} substantive matches")

            # --- Final decision (territory-aware) ---
            # Candidate threshold relaxes to 0.80 when the territory matches (a
            # near-miss spelling in the CORRECT territory is a real candidate),
            # 0.85 otherwise. Auto-adopt requires a strong (>=0.90), UNAMBIGUOUS
            # hit in the correct territory; saint-only never auto-adopts.
            def _meets_candidate(r):
                sc = r["fuzzy_score"] / 100.0
                thr = 0.80 if bool(r["territories_match"]) else 0.85
                return sc >= thr

            def _meets_auto(r):
                saint_ok = r["saint_match"] in ("saint_match", "no_saint")
                return (
                    saint_ok
                    and bool(r["territories_match"])
                    and not r.get("saint_only", False)
                    and (r["fuzzy_score"] / 100.0) >= 0.90
                )

            def _finalize(r, outcome):
                r["phase_1a_outcome"] = outcome
                r.pop("nombre_match", None)
                r.pop("saint_only", None)
                r.pop("had_mismatch", None)
                return r

            candidate_pool = [r for r in row_candidates if _meets_candidate(r)]

            if logger:
                logger.info(f"  Candidate pool (>= threshold): {len(candidate_pool)}")

            if not candidate_pool:
                if logger:
                    logger.warning(f"  No candidate above threshold - relegated")
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
            elif len(candidate_pool) == 1 and _meets_auto(candidate_pool[0]):
                if logger:
                    logger.info(f"  Single unambiguous strong hit -> auto_adopt")
                results.append(_finalize(candidate_pool[0], "auto_adopt"))
            else:
                # Multiple plausible hits (ambiguous) or a single sub-auto hit
                # -> present as candidates, never auto-adopt.
                if logger:
                    logger.info(f"  {len(candidate_pool)} candidate(s) -> candidate")
                for r in candidate_pool:
                    results.append(_finalize(r, "candidate"))
        
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
