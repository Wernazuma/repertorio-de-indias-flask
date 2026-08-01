# main/matching/main.py
import os
import json
import time
import traceback
import pandas as pd
from .saints import load_saints
from .fast_match import build_ref_index, match_batch, collapse_reference
from .filters import ensure_dtype_consistency
from .logging_config import setup_matching_logger, setup_debug_logger


# ---------------------------------------------------------------------------
# Status tracking (JSON, polled by the live progress page)
# ---------------------------------------------------------------------------

REF_SOURCE = os.path.join("data", "reference_gazetteer.csv")
# Derived, one-row-per-place cache of REF_SOURCE (see load_collapsed_reference).
# Safe to delete — it rebuilds automatically.
COLLAPSED_REF_CACHE = os.path.join("data", "reference_gazetteer_collapsed.csv")


def load_collapsed_reference(logger=None):
    """Return the one-row-per-place reference used for Phase-1a matching.

    Collapsing the ~80k-row per-slice gazetteer takes ~14s, so the result is
    cached next to the source and reused until the source changes (compared by
    mtime). Delete the cache (or regenerate the gazetteer) and it rebuilds on
    the next run.
    """
    try:
        if (os.path.exists(COLLAPSED_REF_CACHE)
                and os.path.getmtime(COLLAPSED_REF_CACHE) >= os.path.getmtime(REF_SOURCE)):
            ref = pd.read_csv(COLLAPSED_REF_CACHE, sep=";", encoding="utf-8", low_memory=False)
            if logger:
                logger.info(f"Loaded collapsed reference from cache: {len(ref)} places")
            return ensure_dtype_consistency(ref)
    except Exception as e:  # corrupt/unreadable cache — fall through and rebuild
        if logger:
            logger.warning(f"Collapsed-reference cache unusable ({e}); rebuilding")

    ref = ensure_dtype_consistency(
        pd.read_csv(REF_SOURCE, sep=";", encoding="utf-8", low_memory=False))
    ref = ensure_dtype_consistency(collapse_reference(ref, logger=logger))
    try:
        tmp = COLLAPSED_REF_CACHE + ".tmp"
        ref.to_csv(tmp, sep=";", index=False, encoding="utf-8")
        os.replace(tmp, COLLAPSED_REF_CACHE)  # atomic on same filesystem
        if logger:
            logger.info(f"Wrote collapsed-reference cache: {COLLAPSED_REF_CACHE}")
    except Exception as e:
        if logger:
            logger.warning(f"Could not write collapsed-reference cache ({e})")
    return ref


def _status_path(prefix: str) -> str:
    return os.path.join("data", "uploads", f"{prefix}_status.json")


def _atomic_write_json(path: str, data: dict):
    # Unique temp name so concurrent writers never clash on the same tmp file.
    tmp = f"{path}.{os.getpid()}.{time.time_ns()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f)
    # os.replace is atomic, but on Windows it fails with PermissionError (WinError
    # 5) if the live-progress poller has the destination open for reading at that
    # instant. Progress JSON is non-critical, so retry briefly, then fall back to
    # a direct (non-atomic) write rather than crashing the whole pipeline.
    for _ in range(20):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            time.sleep(0.03)
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f)
    finally:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass


def read_status(prefix: str):
    try:
        with open(_status_path(prefix), "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def read_constraints(prefix: str):
    """Global region/province constraints the user set (optionally BEFORE
    matching), applied to every row in Phase 1a/1b. None when absent."""
    path = os.path.join("data", "uploads", f"{prefix}_constraints.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def _write_status(prefix: str, processed: int, total: int, phase: str = "1a",
                  stage: str = "running"):
    pct = (processed / total * 100) if total else 0.0
    _atomic_write_json(_status_path(prefix), {
        "phase": phase,
        "processed": processed,
        "total": total,
        "percent": round(pct, 1),
        "stage": stage,
        "error": None,
    })


def write_stage(prefix: str, stage: str, phase: str = None, error: str = None):
    """Update the high-level stage/error. Switching to a new phase resets the
    counters so the bar shows 'preparing' until the new phase reports progress."""
    data = read_status(prefix) or {
        "phase": phase or "1a", "processed": 0, "total": 0, "percent": 0.0
    }
    if phase and phase != data.get("phase"):
        data["processed"] = 0
        data["total"] = 0
        data["percent"] = 0.0
    data["stage"] = stage
    if phase:
        data["phase"] = phase
    data["error"] = error
    _atomic_write_json(_status_path(prefix), data)


def run_pipeline(prefix: str, debug_labels=None):
    """Run Phase 1a then Phase 1b back-to-back (intended for a background thread).

    Progress is reported through the status JSON; the final stage is "done"
    (or "error"), which the live progress page uses to auto-advance.
    """
    try:
        write_stage(prefix, "running", phase="1a")
        match_phase_1(prefix, debug_labels=debug_labels)
        write_stage(prefix, "running", phase="1b")
        match_phase_1b(prefix)
        write_stage(prefix, "done")
    except Exception as e:
        traceback.print_exc()
        write_stage(prefix, "error", error=str(e))


# Territorial fields blanked at each backoff step (cumulative)
_BACKOFF_STEPS = [
    {
        "label": "no_partido_jurisdiccion",
        "blank": ["ref_Partido", "ref_partido", "ref_Jurisdiccion"],
    },
    {
        "label": "no_provincia",
        "blank": ["ref_Partido", "ref_partido", "ref_Jurisdiccion",
                  "ref_Provincia", "ref_Provincia_menor", "ref_ProvinciaMenor"],
    },
    {
        "label": "no_provincia_mayor",
        "blank": ["ref_Partido", "ref_partido", "ref_Jurisdiccion",
                  "ref_Provincia", "ref_Provincia_menor", "ref_ProvinciaMenor",
                  "ref_Provincia_mayor", "ref_ProvinciaMayor", "ref_Obispado"],
    },
]

def match_phase_1(prefix, debug_labels=None):
    """
    Run Phase 1 matching.
    
    Args:
        prefix: File prefix for input/output
        debug_labels: Optional list of ref_labels to debug in detail
                     e.g., ['otavalo', 'veracruz', 'guayaquil', 'méxico']
    """
    upload_dir = os.path.join("data", "uploads")
    inp = pd.read_csv(os.path.join(upload_dir, f"{prefix}_cleaned.csv"), sep=";", encoding="utf-8")

    # Set up loggers
    logger = setup_matching_logger(prefix)
    logger.info(f"Input rows: {len(inp)}")

    # Phase-1a matching is time-agnostic, so use the one-row-per-place collapse
    # (cached on disk, rebuilt only when the source gazetteer changes).
    ref = load_collapsed_reference(logger=logger)
    logger.info(f"Reference gazetteer rows: {len(ref)}")
    
    # Set up debug logger if specific labels requested
    debug_logger = None
    if debug_labels:
        debug_logger = setup_debug_logger(prefix, debug_labels)
        logger.info(f"Debug mode enabled for labels: {debug_labels}")
        
        # DIAGNOSTIC: Check if debug labels exist in reference
        from .cleaning import clean_toponym
        debug_logger.info(f"\n{'='*80}")
        debug_logger.info(f"PRE-MATCH DIAGNOSTIC: Checking reference for debug labels")
        debug_logger.info(f"{'='*80}")
        for label in debug_labels:
            label_cleaned = clean_toponym(label)
            debug_logger.info(f"\nSearching for '{label}' (cleaned: '{label_cleaned}') in reference:")
            
            # Check exact matches
            exact_label = ref[ref['lugar_label'].str.lower() == label.lower()]
            exact_nombre = ref[ref['lugar_nombre'].str.lower() == label.lower()]
            
            # Check cleaned matches
            ref_labels_cleaned = ref['lugar_label'].apply(lambda x: clean_toponym(str(x)) if pd.notna(x) else "")
            ref_nombres_cleaned = ref['lugar_nombre'].apply(lambda x: clean_toponym(str(x)) if pd.notna(x) else "")
            
            matches_label = ref[ref_labels_cleaned == label_cleaned]
            matches_nombre = ref[ref_nombres_cleaned == label_cleaned]
            
            debug_logger.info(f"  Exact label matches (case-insensitive): {len(exact_label)}")
            debug_logger.info(f"  Exact nombre matches (case-insensitive): {len(exact_nombre)}")
            debug_logger.info(f"  Cleaned label matches: {len(matches_label)}")
            debug_logger.info(f"  Cleaned nombre matches: {len(matches_nombre)}")
            
            if len(matches_label) > 0 or len(matches_nombre) > 0:
                debug_logger.info(f"  Sample matches:")
                for _, row in matches_label.head(5).iterrows():
                    debug_logger.info(f"    gz_id={row['gz_id']}: label='{row['lugar_label']}' adm0={row.get('adm0_iso', 'N/A')}")
                for _, row in matches_nombre.head(5).iterrows():
                    debug_logger.info(f"    gz_id={row['gz_id']}: nombre='{row['lugar_nombre']}' adm0={row.get('adm0_iso', 'N/A')}")

    saints = load_saints(os.path.join("data", "santos.csv"))
    logger.info(f"Loaded {len(saints)} saints")
    
    ref_index = build_ref_index(ref, logger=logger)
    constraints = read_constraints(prefix)
    if constraints:
        logger.info(f"Applying global constraints: {constraints}")

    batch_size = 100
    total = len(inp)
    results = []
    for start in range(0, total, batch_size):
        end = start + batch_size
        try:
            batch_res = match_batch(
                inp, ref, ref_index, saints, start, end,
                logger=logger,
                debug_logger=debug_logger,
                target_labels=debug_labels,
                constraints=constraints,
            )
            results.extend(batch_res)
            _write_status(prefix, end if end < total else total, total)
            logger.info(f"Batch {start}-{end}: completed successfully, {len(batch_res)} results")
        except Exception as e:
            logger.error(f"ERROR in batch {start}-{end}: {e}")
            logger.error(f"Exception type: {type(e).__name__}")
            import traceback
            logger.error(traceback.format_exc())
            raise

    logger.info(f"Total result records: {len(results)}")

    # Convert results to DataFrame and fix fuzzy_score formatting
    results_df = pd.DataFrame(results)
    if 'fuzzy_score' in results_df.columns:
        # Convert from 0-100 scale to 0.0-1.0 scale
        results_df['fuzzy_score'] = results_df['fuzzy_score'].apply(
            lambda x: round(x / 100, 3) if pd.notna(x) else x
        )

    # Merge context columns from input (all columns not already in results)
    ctx_cols = [c for c in inp.columns if c not in results_df.columns and c != "rowID"]
    if ctx_cols:
        ctx = inp[["rowID"] + ctx_cols].drop_duplicates(subset=["rowID"])
        results_df = results_df.merge(ctx, on="rowID", how="left")

    out_path = os.path.join(upload_dir, f"{prefix}_processing.csv")
    results_df.to_csv(out_path, sep=";", index=False, encoding="utf-8")
    open(os.path.join(upload_dir, f"{prefix}_phase1a_complete.flag"), "w").close()

    # --- ✨ Post-process CSV text: strip trailing .0 from numbers ---
    with open(out_path, "r", encoding="utf-8") as f:
        content = f.read()
    cleaned = []
    for line in content.splitlines():
        parts = []
        for cell in line.split(";"):
            if (
                cell.replace(".", "", 1).isdigit()
                and cell.endswith(".0")
                and cell.lower() not in ["lat", "lon"]
            ):
                cell = cell[:-2]
            parts.append(cell)
        cleaned.append(";".join(parts))
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(cleaned))

    logger.info(f"=" * 80)
    logger.info(f"Matching process completed successfully")
    logger.info(f"=" * 80)

    return out_path


def match_phase_1b(prefix):
    """
    Phase 1b: backoff matching for relegated rows.

    For each relegated row, re-runs match_batch with progressively blanked
    territorial fields (cumulative per step):
      Step 1 — blank ref_Partido / ref_Jurisdiccion
      Step 2 — also blank ref_Provincia / ref_Provincia_menor
      Step 3 — also blank ref_Provincia_mayor / ref_Obispado

    The same strict territory_match rules apply at every step: a mismatch
    on the finest still-present field relegates that candidate immediately.
    A row stops at the first step that yields non-relegated results.
    Rows with no result after all steps remain relegated.
    """
    upload_dir = os.path.join("data", "uploads")
    processing_path = os.path.join(upload_dir, f"{prefix}_processing.csv")

    processing_df = pd.read_csv(processing_path, sep=";", encoding="utf-8")
    inp = pd.read_csv(os.path.join(upload_dir, f"{prefix}_cleaned.csv"), sep=";", encoding="utf-8")

    logger = setup_matching_logger(prefix)
    ref = load_collapsed_reference(logger=logger)
    saints = load_saints(os.path.join("data", "santos.csv"))
    ref_index = build_ref_index(ref, logger=logger)
    constraints = read_constraints(prefix)

    relegated_ids = (
        processing_df[processing_df["phase_1a_outcome"] == "relegated"]["rowID"]
        .unique().tolist()
    )
    total = len(relegated_ids)
    logger.info(f"Phase 1b: {total} relegated rows to process")

    if total == 0:
        logger.info("Phase 1b: nothing to do.")
        open(os.path.join(upload_dir, f"{prefix}_phase1b_complete.flag"), "w").close()
        return processing_path, 0

    new_results = []
    fixed_ids = []

    for i, row_id in enumerate(relegated_ids):
        _write_status(prefix, i + 1, total, phase="1b")

        input_rows = inp[inp["rowID"] == row_id]
        if input_rows.empty:
            continue

        for step in _BACKOFF_STEPS:
            modified = input_rows.copy()
            for field in step["blank"]:
                if field in modified.columns:
                    modified[field] = None

            batch = match_batch(modified, ref, ref_index, saints, logger=logger,
                                 constraints=constraints)
            valid = [r for r in batch if r.get("phase_1a_outcome") != "relegated"]

            if valid:
                # Check if the original 1a-relegated row had a fine-level territory mismatch.
                # If so, even a successful 1b match must not become auto_adopt —
                # the input's fine-level field contradicts the gazetteer.
                orig_rows = processing_df[processing_df["rowID"] == row_id]
                had_fine_mismatch = False
                if not orig_rows.empty:
                    raw = orig_rows.iloc[0].get("fine_level_mismatch", False)
                    had_fine_mismatch = str(raw).strip().lower() in ("true", "1")

                for r in valid:
                    r["phase_1b_step"] = step["label"]
                    # fix fuzzy_score scale
                    if r.get("fuzzy_score") is not None:
                        r["fuzzy_score"] = round(r["fuzzy_score"] / 100, 3)
                    if had_fine_mismatch and r.get("phase_1a_outcome") == "auto_adopt":
                        r["phase_1a_outcome"] = "candidate"
                new_results.extend(valid)
                fixed_ids.append(row_id)
                logger.info(f"  rowID={row_id}: found at step '{step['label']}'")
                break

    # Remove old relegated placeholders for fixed rows, append new results
    if fixed_ids:
        mask = (processing_df["rowID"].isin(fixed_ids)) & (processing_df["phase_1a_outcome"] == "relegated")
        processing_df = processing_df[~mask].copy()

        new_df = pd.DataFrame(new_results)

        # Carry context columns from input
        ctx_cols = [c for c in inp.columns if c not in new_df.columns and c != "rowID"]
        if ctx_cols:
            ctx = inp[["rowID"] + ctx_cols].drop_duplicates(subset=["rowID"])
            new_df = new_df.merge(ctx, on="rowID", how="left")

        processing_df = pd.concat([processing_df, new_df], ignore_index=True)

    processing_df.to_csv(processing_path, sep=";", index=False, encoding="utf-8")
    open(os.path.join(upload_dir, f"{prefix}_phase1b_complete.flag"), "w").close()

    logger.info(f"Phase 1b complete: {len(fixed_ids)} of {total} relegated rows recovered")
    return processing_path, len(fixed_ids)
