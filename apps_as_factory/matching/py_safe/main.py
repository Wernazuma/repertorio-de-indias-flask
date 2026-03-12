# main/matching/main.py
import os
import pandas as pd
from .saints import load_saints
from .fast_match import build_ref_index, match_batch
from .filters import ensure_dtype_consistency
from .logging_config import setup_matching_logger  # ADD THIS LINE


def _write_status(prefix: str, processed: int, total: int):
    """Write progress to the Flask-visible status file."""
    status_file = os.path.join("data", "uploads", f"{prefix}_status.txt")
    with open(status_file, "w", encoding="utf-8") as f:
        pct = (processed / total) * 100
        f.write(f"Phase 1a: Processed {processed} of {total} rows ({pct:.1f}%)\n")

def match_phase_1(prefix):
    upload_dir = os.path.join("data", "uploads")
    inp = pd.read_csv(os.path.join(upload_dir, f"{prefix}_cleaned.csv"), sep=";", encoding="utf-8")
    ref = pd.read_csv(os.path.join("data", "reference_gazetteer.csv"), sep=";", encoding="utf-8")
    ref = ensure_dtype_consistency(ref)

    # Set up logger - ADD THIS SECTION
    #logger = setup_matching_logger(prefix)
    #logger.info(f"Input rows: {len(inp)}")
    #logger.info(f"Reference gazetteer rows: {len(ref)}")

    saints = load_saints(os.path.join("data", "santos.csv"))
    #logger.info(f"Loaded {len(saints)} saints")  # ADD THIS LINE
    
    ref_index = build_ref_index(ref)
    #logger.info(f"Built reference index with {len(ref_index)} tokens")  # ADD THIS LINE

    batch_size = 100
    total = len(inp)
    results = []
    for start in range(0, total, batch_size):
        end = start + batch_size
        batch_res = match_batch(inp, ref, ref_index, saints, start, end, logger)  # ADD logger PARAMETER
        results.extend(batch_res)
        _write_status(prefix, end if end < total else total, total)

    #logger.info(f"Total result records: {len(results)}")  # ADD THIS LINE
    
    out_path = os.path.join(upload_dir, f"{prefix}_processing.csv")
    pd.DataFrame(results).to_csv(out_path, sep=";", index=False, encoding="utf-8")
    open(os.path.join(upload_dir, f"{prefix}_phase1a_complete.flag"), "w").close()

    # --- ✨ Post-process CSV text: strip trailing .0 from numbers ---
    with open(out_path, "r", encoding="utf-8") as f:
        content = f.read()
    # remove any .0 that appears at end of a number (but not decimal coordinates)
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

    #logger.info(f"=" * 80)  # ADD THIS LINE
    #logger.info(f"Matching process completed successfully")  # ADD THIS LINE
    #logger.info(f"=" * 80)  # ADD THIS LINE
    
    return out_path
