# main/matching/main.py
import os
import pandas as pd
from .saints import load_saints
from .fast_match import build_ref_index, match_batch
from .filters import ensure_dtype_consistency
from .logging_config import setup_matching_logger, setup_debug_logger


def _write_status(prefix: str, processed: int, total: int):
    """Write progress to the Flask-visible status file."""
    status_file = os.path.join("data", "uploads", f"{prefix}_status.txt")
    with open(status_file, "w", encoding="utf-8") as f:
        pct = (processed / total) * 100
        f.write(f"Phase 1a: Processed {processed} of {total} rows ({pct:.1f}%)\n")

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
    ref = pd.read_csv(os.path.join("data", "reference_gazetteer.csv"), sep=";", encoding="utf-8")
    ref = ensure_dtype_consistency(ref)

    # Set up loggers
    logger = setup_matching_logger(prefix)
    logger.info(f"Input rows: {len(inp)}")
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
                target_labels=debug_labels
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
