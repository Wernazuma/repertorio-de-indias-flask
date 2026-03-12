import os, threading, time, re, logging, traceback
import pandas as pd
from flask import Flask, request, render_template, redirect, url_for, flash, jsonify, current_app
from collections import defaultdict
from fuzzywuzzy import fuzz
from disambiguation import disambiguate_candidates
from logging.handlers import RotatingFileHandler
from . import bp

# Setup logging to file
log_folder = os.path.join("data", "logs")
os.makedirs(log_folder, exist_ok=True)

log_file_path = os.path.join(log_folder, "matching.log")

file_handler = RotatingFileHandler(log_file_path, maxBytes=5_000_000, backupCount=3)
file_handler.setLevel(logging.DEBUG)
formatter = logging.Formatter('%(asctime)s %(levelname)s: %(message)s')
file_handler.setFormatter(formatter)

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)
logger.addHandler(file_handler)



#app = Flask(__name__)
#app.secret_key = 'your_secret_key'

UPLOAD_FOLDER = os.path.join("data", "uploads")
GAZETTEER_FILE = os.path.join("data", "espartede.csv")
PATRON_FILE = os.path.join("data", "santos.csv")




def load_entidades_csv():
    path = os.path.join("data", "gz_entidades.csv")
    df = pd.read_csv(path, sep=';', encoding='utf-8')
    df.columns = df.columns.str.lower()
    return df

# ---------------------------
# Cleaning Functions
# ---------------------------
def normalize(inputstring):
    if pd.isna(inputstring) or inputstring == '':
        return ''
    inputstring = re.sub(r'[\.,;:]', ' ', str(inputstring))
    replacements = [
        ('á', 'a'), ('é', 'e'), ('í', 'i'), ('ó', 'o'), ('ú', 'u'),
        ('vv', 'w'), ('v', 'b'), ('tz', 'z'), ('y', 'i'), ('z', 's'), ('ñ', 'n')
    ]
    for old, new in replacements:
        inputstring = re.sub(old, new, inputstring)
    inputstring = re.sub(r'x([aeiouáéíóú])', r'j\1', inputstring)
    inputstring = re.sub(r'g([éeií])', r'j\1', inputstring)
    inputstring = re.sub(r'gu', 'hu', inputstring)
    inputstring = re.sub(r's([éeíi])', r'c\1', inputstring)
    inputstring = re.sub(r'([^c])h([aeiouáéíóú])([^eaéá])', r'\1\2\3', inputstring)
    inputstring = re.sub(r'(Sta )', 'Santa ', inputstring)
    inputstring = re.sub(r'(Sto )', 'Santo ', inputstring)
    inputstring = re.sub(r'(Sn )', 'San ', inputstring)
    inputstring = inputstring.lower()
    inputstring = re.sub(r'  +', ' ', inputstring)
    inputstring = re.sub(r'-', ' ', inputstring)
    return inputstring.strip()

def stopwordremove(inputstring):
    if pd.isna(inputstring) or inputstring == '':
        return ''
    inputstring = re.sub(r'(?<!\w)(de|la|el|del|las|los)(?!\w)', ' ', inputstring, flags=re.IGNORECASE)
    inputstring = re.sub(r'Nuestra Señora', '', inputstring, flags=re.IGNORECASE)
    inputstring = re.sub(r'NS', '', inputstring)
    inputstring = re.sub(r'  +', ' ', inputstring)
    return inputstring.strip()

def clean_toponym(toponym):
    if pd.isna(toponym) or toponym == '':
        return ''
    return stopwordremove(normalize(str(toponym)))

def load_patron_saints():
    try:
        santos_df = pd.read_csv(PATRON_FILE, delimiter=';', encoding='utf-8')
        saints = santos_df['santo'].dropna().unique().tolist()
        saints.sort(key=len, reverse=True)
        return saints
    except Exception as e:
        print(f"Error loading santos.csv: {e}")
        return []

REGION_CODE_TO_NAME = {
    "GDJ": "Nueva Galicia y Septentrion",
    "NES": "Nueva España",
    "GUA": "Guatemala",
    "SDO": "Santo Domingo",
    "VEN": "Venezuela",
    "TFI": "Tierra Firme",
    "NGR": "Nuevo Reino de Granada",
    "QUI": "Quito",
    "PER": "Peru",
    "CHA": "Charcas",
    "CHL": "Chile",
    "RPL": "Rio de la Plata",
    "FIL": "Filipinas",
    "EXT": "Exterior"
}



def create_match_result(input_row, row, toponym_match, score, patron_saints):
    """Create a result dictionary for a matched input row against a gazetteer row."""
    ref_label = input_row.get("ref_Label", "")
    
    # Extract scalar values from row (handles both Series and dict)
    def get_value(row, key, default=""):
        val = row.get(key, default)
        # If it's a Series, extract the scalar value
        if isinstance(val, pd.Series):
            return val.iloc[0] if len(val) > 0 else default
        return val
    
    # --- Saint match ---
    saint_match = "saint_null"
    for saint in patron_saints:
        if saint.lower() in ref_label.lower():
            lugar_santo = get_value(row, "lugar_santo", "")
            if pd.notna(lugar_santo) and saint.lower() in str(lugar_santo).lower():
                saint_match = "saint_match"
            else:
                saint_match = "saint_mismatch"
            break
    
    # --- Category match ---
    ref_cat = input_row.get("ref_categoria")
    if ref_cat:
        lugar_cat = get_value(row, "lugar_categoria", "")
        lugar_cat_esp = get_value(row, "lugar_categoria_especial", "")
        cat_match = (
            "category_match"
            if ref_cat in [lugar_cat, lugar_cat_esp]
            else "category_mismatch"
        )
    else:
        cat_match = "category_null"
    
    # --- Extract gz_id and convert to int ---
    gz_id = get_value(row, "gz_id", None)
    if pd.notna(gz_id):
        gz_id = int(gz_id)
    else:
        gz_id = None
    
    # --- Core result structure ---
    result = {
        "rowID": input_row.get("rowID"),
        "ref_Label": ref_label,
        "gz_id": gz_id,
        "lugar_nombre": get_value(row, "lugar_nombre", ""),
        "lugar_variantes": get_value(row, "lugar_variantes", ""),
        "toponym-match": toponym_match,
        "toponym-score": score,
        "saint-match": saint_match,
        "category-match": cat_match,
        "territories-match": "matched",
        "phase-1-outcome": "candidate"
    }
    
    # --- Add all other ref_ fields dynamically (only base columns, no _x/_y) ---
    for key in input_row.index:
        if key.startswith("ref_") and key not in result and not key.endswith("_x") and not key.endswith("_y"):
            result[key] = input_row.get(key)
    
    return result


def is_high_confidence(result):
    """Check if result is high confidence"""
    return (result["toponym-match"] in ["toponym_nombre", "toponym_label"] and 
            result["saint-match"] in ["saint_match", "saint_null"] and 
            result["category-match"] in ["category_match", "category_null"])

def match_toponyms_tiered(input_row, filtered_df, patron_saints):
    """Tiered matching: exact -> cleaned -> fuzzy"""
    ref_label = input_row.get("ref_Label", "")
    results = []
    
    # TIER 1: Exact matches (no cleaning needed)
    logger.debug(f"[{input_row.get('rowID')}] TIER 1: Checking exact matches")
    for idx, row in filtered_df.iterrows():
        # Check exact matches on raw strings first
        if (str(row["lugar_label"]).lower() == ref_label.lower() or
            str(row["lugar_nombre"]).lower() == ref_label.lower()):
            
            toponym_match = "toponym_label" if str(row["lugar_label"]).lower() == ref_label.lower() else "toponym_nombre"
            result = create_match_result(input_row, row, toponym_match, 1, patron_saints)
            
            # Early exit for high confidence
            if is_high_confidence(result):
                result["phase-1-outcome"] = "auto_adopt"
                logger.info(f"[{input_row.get('rowID')}] TIER 1: High confidence exact match found")
                return [result]
            
            results.append(result)
    
    # If we found exact matches, return them
    if results:
        logger.info(f"[{input_row.get('rowID')}] TIER 1: Found {len(results)} exact matches")
        return results
    
    # TIER 2: Cleaned matches (only if no exact matches)
    logger.debug(f"[{input_row.get('rowID')}] TIER 2: Checking cleaned matches")
    cleaned_label = clean_toponym(ref_label)
    
    for idx, row in filtered_df.iterrows():
        if clean_toponym(row["lugar_label"]) == cleaned_label:
            result = create_match_result(input_row, row, "toponym_label", 1, patron_saints)
            if is_high_confidence(result):
                result["phase-1-outcome"] = "auto_adopt"
                return [result]
            results.append(result)
        elif clean_toponym(row["lugar_nombre"]) == cleaned_label:
            result = create_match_result(input_row, row, "toponym_nombre", 1, patron_saints)
            if is_high_confidence(result):
                result["phase-1-outcome"] = "auto_adopt"
                return [result]
            results.append(result)
    
    if results:
        return results
    
    # TIER 3: Variants and fuzzy matching (most expensive, last resort)
    logger.debug(f"[{input_row.get('rowID')}] TIER 3: Checking variants and fuzzy matches")
    for idx, row in filtered_df.iterrows():
        if pd.notna(row.get("lugar_variantes")):
            variants = [clean_toponym(v.strip()) for v in row["lugar_variantes"].split(" \\ ")]
            
            # Exact variant match
            if cleaned_label in variants:
                result = create_match_result(input_row, row, "toponym_variante", 1, patron_saints)
                if is_high_confidence(result):
                    result["phase-1-outcome"] = "auto_adopt"
                    return [result]
                results.append(result)
            else:
                # Fuzzy matching on variants
                best_score = 0
                for variant in variants:
                    lev_score = fuzz.ratio(cleaned_label, variant) / 100
                    if lev_score >= 0.85 and lev_score > best_score:
                        best_score = lev_score
                        result = create_match_result(input_row, row, "toponym_levenshtein", best_score, patron_saints)
                        if is_high_confidence(result):
                            result["phase-1-outcome"] = "auto_adopt"
                            return [result]
                        results.append(result)
                        break  # Only keep the best fuzzy match per row
    
    return results


# ---------------------------
# Matching Logic
# ---------------------------
def match_row_to_gazetteer(input_row, gazetteer_df, patron_saints):
    """
    Optimized matching function for large-scale gazetteers.
    Avoids inner loops and excessive row-wise apply calls.
    """
    results = []

    ref_label = input_row.get("ref_Label", "")
    cleaned_label = clean_toponym(ref_label)
    row_id = input_row.get("rowID")
    logger.debug(f"[{row_id}] ref_Label: {ref_label} → cleaned: {cleaned_label}")

    spatial_fields = [
        "ref_Partido_generico", "ref_Partido", "ref_Jurisdiccion",
        "ref_Provincia", "ref_Provincia_menor", "ref_Provincia_mayor",
        "ref_Obispado", "ref_Audiencia", "ref_Region", "ref_Pais"
    ]

    has_spatial_info = any(
        pd.notna(input_row.get(f)) and str(input_row.get(f)).strip() != ""
        for f in spatial_fields
    )

    if not has_spatial_info:
        logger.warning(f"[{row_id}] No usable spatial fields. Relegating early.")
        return [{
            "rowID": row_id,
            "ref_Label": ref_label,
            "gz_id": None,
            "toponym-match": "null",
            "toponym-score": 0,
            "saint-match": "null",
            "category-match": "null",
            "territories-match": "missing_spatial_fields",
            "phase-1-outcome": "relegated"
        }]

    # Pre-filter gazetteer only once
    filtered_df = gazetteer_df.copy()

    # Apply generic territory filters
    for field in ["ref_Partido_generico", "ref_Provincia_generica", "ref_Region", "ref_Pais"]:
        value = input_row.get(field)
        if pd.notna(value) and value != "":
            col = field.replace("ref_", "lugar_").lower()
            if field == "ref_Region":
                value = REGION_CODE_TO_NAME.get(value, value)
            if col in filtered_df.columns:
                filtered_df = filtered_df[filtered_df[col] == value]

    logger.debug(f"[{row_id}] After generic territory filter: {len(filtered_df)} rows")

    # Time filtering
    year = None
    if pd.notna(input_row.get("ref_Year")):
        year = int(input_row["ref_Year"])
    elif pd.notna(input_row.get("ref_START")) and pd.notna(input_row.get("ref_END")):
        year = (int(input_row["ref_START"]) + int(input_row["ref_END"])) // 2

    if year:
        year = min(max(year, 1701), 1808)
        if 'overlap_start' in filtered_df.columns and 'overlap_end' in filtered_df.columns:
            filtered_df = filtered_df[
                (filtered_df['overlap_start'] <= year) & (filtered_df['overlap_end'] >= year)
            ]

    logger.debug(f"[{row_id}] After time filter: {len(filtered_df)} rows")

    # Define hierarchical territorial levels
    territory_levels = [
        {
            "fields": {
                "ref_Partido": "Partido",
                "ref_Jurisdiccion": "Jurisdiccion"
            },
            "level_name": "partido/jurisdiccion"
        },
        {
            "fields": {
                "ref_Provincia": "Provincia",
                "ref_Provincia_menor": "Provincia menor"
            },
            "level_name": "provincia"
        },
        {
            "fields": {
                "ref_Provincia_mayor": "Provincia mayor",
                "ref_Obispado": "Obispado"
            },
            "level_name": "provincia_mayor/obispado"
        },
        {
            "fields": {
                "ref_Audiencia": "Audiencia"
            },
            "level_name": "audiencia"
        }
    ]

    for level_config in territory_levels:
        level_has_data = any(
            pd.notna(input_row.get(field)) and str(input_row.get(field)).strip() != ""
            for field in level_config["fields"]
        )
        if not level_has_data:
            continue

        level_df = filtered_df.copy()
        logger.info(f"[{row_id}] Trying level: {level_config['level_name']}")

        # Apply specific territory filters using vectorized boolean masks
        for field, expected_nivel in level_config["fields"].items():
            value = input_row.get(field)
            if pd.isna(value) or value == "":
                continue

            cleaned_value = clean_toponym(value)

            # Create boolean masks for matches
            mask_nivel = level_df["Nivel"] == expected_nivel

            # Fuzzy match variants & names in vectorized form
            candidate_cols = ["polygon_label", "polygon_nombre", "polygon_variantes"]
            match_mask = pd.Series(False, index=level_df.index)

            for col in candidate_cols:
                if col not in level_df.columns:
                    continue

                col_series = level_df[col].fillna("").astype(str)
                if col == "polygon_variantes":
                    variant_mask = col_series.str.contains(cleaned_value, case=False, regex=False)
                else:
                    variant_mask = col_series.str.contains(cleaned_value, case=False, regex=False)

                match_mask |= variant_mask

            level_df = level_df[mask_nivel & match_mask]

        if level_df.empty:
            logger.debug(f"[{row_id}] No matches at level {level_config['level_name']}")
            continue

        # First-letter optimization
        if cleaned_label:
            first_letter = cleaned_label[0].lower()
            name_cols = ["lugar_label", "lugar_nombre", "lugar_variantes"]
            starts_with_letter_mask = pd.Series(False, index=level_df.index)

            for col in name_cols:
                if col not in level_df.columns:
                    continue
                col_series = level_df[col].fillna("").astype(str).str.lower()
                starts_with_letter_mask |= col_series.str.startswith(first_letter)

            same_letter_df = level_df[starts_with_letter_mask]
            other_letter_df = level_df[~starts_with_letter_mask]

            # Try same-letter subset first
            for subset_df in [same_letter_df, other_letter_df]:
                if subset_df.empty:
                    continue

                level_results = match_toponyms_tiered(input_row, subset_df, patron_saints)
                if level_results and level_results[0].get("phase-1-outcome") == "auto_adopt":
                    return level_results
                if level_results and level_results[0].get("toponym-match") != "null":
                    results.extend(level_results)
                    break
        else:
            level_results = match_toponyms_tiered(input_row, level_df, patron_saints)
            if level_results and level_results[0].get("phase-1-outcome") == "auto_adopt":
                return level_results
            if level_results and level_results[0].get("toponym-match") != "null":
                results.extend(level_results)
                break

        if results:
            break

    if not results:
        return [{
            "rowID": row_id,
            "ref_Label": ref_label,
            "gz_id": None,
            "toponym-match": "null",
            "toponym-score": 0,
            "saint-match": "null",
            "category-match": "null",
            "territories-match": "no_matches_all_levels",
            "phase-1-outcome": "relegated"
        }]

    return results



def phase_1b_backoff(prefix):
    """
    Phase 1b (time-first backoff):
    For rows that are 'relegated' (no candidates from Phase 1),
    1) remove time constraint (ref_Year/ref_START/ref_END) and retry;
    2) if still none, progressively drop smallest ref territories:
       - drop ref_Partido/ref_Jurisdiccion
       - then drop ref_Provincia/ref_Provincia_menor
       - then drop ref_Provincia_mayor/ref_Obispado
       Final scope leaves Audiencia (we do NOT drop ref_Audiencia).
    Writes results back into <prefix>_processing.csv in-place.
    """
    status_file = os.path.join(UPLOAD_FOLDER, f"{prefix}_status.txt")
    processing_path = os.path.join(UPLOAD_FOLDER, f"{prefix}_processing.csv")

    if not os.path.exists(processing_path):
        return

    try:
        processing_df = pd.read_csv(processing_path, sep=";", encoding="utf-8")
        gazetteer_df = pd.read_csv(GAZETTEER_FILE, delimiter=';', decimal=',', encoding='utf-8')
        patron_saints = load_patron_saints()
    except Exception as e:
        logger.error(f"[Phase 1b] Error loading files: {e}")
        try:
            with open(status_file, "w", encoding="utf-8") as f:
                f.write(f"Phase 1b error: {e}\n")
        except Exception:
            pass
        return

    # RowIDs that only have 'relegated' rows (no candidates yet)
    grp = processing_df.groupby("rowID")["phase-1-outcome"]
    needs_backoff = grp.apply(lambda s: all(v == "relegated" for v in s.dropna().unique()))
    target_ids = needs_backoff[needs_backoff].index.tolist()

    if not target_ids:
        try:
            with open(status_file, "w", encoding="utf-8") as f:
                f.write("Phase 1b (time-first): no rows required backoff. Proceed to disambiguation.\n")
        except Exception:
            pass
        return

    ref_cols = [c for c in processing_df.columns if c.startswith("ref_")]
    total_targets = len(target_ids)

    # --- Time-first, then territory relax (cumulative blanking) -------------
    time_fields = ["ref_Year", "ref_START", "ref_END"]
    relax_steps = [
        {"label": "time", "blank": time_fields},
        {"label": "partido/jurisdiccion", "blank": ["ref_Partido", "ref_Jurisdiccion"]},
        {"label": "provincia", "blank": ["ref_Provincia", "ref_Provincia_menor"]},
        {"label": "provincia_mayor/obispado", "blank": ["ref_Provincia_mayor", "ref_Obispado"]},
        # Note: keep ref_Audiencia as the final scope
    ]
    # ------------------------------------------------------------------------

    appended = 0
    fixed_ids = []

    for idx, rid in enumerate(target_ids, start=1):
        # progress
        try:
            with open(status_file, "w", encoding="utf-8") as f:
                pct = (idx / total_targets) * 100
                f.write(f"Phase 1b (time-first): {idx}/{total_targets} (rowID={rid}) [{pct:.1f}%]\n")
        except Exception:
            pass

        # Seed the Series the matcher expects
        base_row = processing_df.loc[processing_df["rowID"] == rid].iloc[0].copy()
        seed_keys = ["rowID", "ref_Label"] + ref_cols
        seed = {k: base_row[k] if k in base_row else pd.NA for k in seed_keys}
        working = pd.Series(seed)

        got_candidates = False

        # Cumulative relax: keep prior blanks when moving to the next step
        for step in relax_steps:
            # blank the step fields
            for f in step["blank"]:
                if f in working.index:
                    # Use pd.NA (not ""), so match_row_to_gazetteer won't try int("") on time fields
                    working[f] = pd.NA
            # Try match with the current relaxed constraints
            row_matches = match_row_to_gazetteer(working, gazetteer_df, patron_saints)

            # Any candidates (i.e., not the lone relegated placeholder)?
            if row_matches and not (len(row_matches) == 1 and row_matches[0].get("phase-1-outcome") == "relegated"):
                # Remove prior relegated placeholder(s) for this rowID
                processing_df = processing_df[~((processing_df["rowID"] == rid) & (processing_df["phase-1-outcome"] == "relegated"))]
                # Append new candidates
                add_df = pd.DataFrame(row_matches)
                # Normalize gz_id dtype (nullable int)
                if "gz_id" in add_df.columns:
                    add_df["gz_id"] = pd.to_numeric(add_df["gz_id"], errors="coerce").astype("Int64")

                processing_df = pd.concat([processing_df, add_df], ignore_index=True)
                appended += len(add_df)
                fixed_ids.append(rid)
                got_candidates = True
                logger.info(f"[Phase 1b] rowID={rid}: candidates found after relaxing: {step['label']}")
                break

        if not got_candidates:
            logger.debug(f"[Phase 1b] rowID={rid}: still relegated after time + territory relax")

    # Normalize gz_id dtype across the whole file for consistency
    if "gz_id" in processing_df.columns:
        processing_df["gz_id"] = pd.to_numeric(processing_df["gz_id"], errors="coerce").astype("Int64")

    # Preserve your column ordering (core first, then ref_*, then manual_*, then rest)
    core_order = [
        "rowID", "ref_Label", "gz_id", "lugar_nombre", "lugar_variantes",
        "toponym-match", "toponym-score", "saint-match", "category-match",
        "territories-match", "phase-1-outcome"
    ]
    existing_core = [c for c in core_order if c in processing_df.columns]
    remaining = [c for c in processing_df.columns if c not in existing_core]
    ref_columns = [c for c in remaining if c.startswith("ref_")]
    manual_columns = [c for c in remaining if c.startswith("manual_")]
    others = [c for c in remaining if c not in ref_columns and c not in manual_columns]

    ordered = existing_core + ref_columns + manual_columns + others
    processing_df = processing_df[ordered]

    try:
        processing_df.to_csv(processing_path, sep=";", index=False, encoding="utf-8")
        with open(status_file, "w", encoding="utf-8") as f:
            f.write(
                f"Phase 1b (time-first) complete. Relaxed {len(fixed_ids)} rowIDs; "
                f"appended {appended} candidate rows. Proceed to disambiguation.\n"
            )
        logger.info(f"Phase 1b (time-first) saved to {processing_path}. Updated {len(fixed_ids)} rows.")
    except Exception as e:
        logger.error(f"[Phase 1b] Error writing processing CSV: {e}")
        try:
            with open(status_file, "w", encoding="utf-8") as f:
                f.write(f"Phase 1b error saving processing CSV: {e}\n")
        except Exception:
            pass



# ---------------------------
# Route: Upload + detection
# ---------------------------
@bp.route("/", methods=["GET", "POST"])
def index():
    prefix = ''
    if request.method == "POST":
        prefix = request.form.get("prefix", "").strip()
        file_path = os.path.join(UPLOAD_FOLDER, f"{prefix}_cleaned.csv")
        if not os.path.exists(file_path):
            flash(f"File not found: {file_path}")
            return render_template("upload_form.html", prefix=prefix)

        try:
            df = pd.read_csv(file_path, delimiter=';', decimal=',', encoding='utf-8')
        except Exception as e:
            flash(f"Failed to read file: {e}")
            return render_template("upload_form.html", prefix=prefix)

        flash(f"File loaded with {len(df)} rows.")
        return render_template("upload_form.html", prefix=prefix)

    return render_template("upload_form.html", prefix=prefix)


# ---------------------------
# Route: Matching Phase 1 (improved processing CSV creation)
# ---------------------------
@bp.route("/match_phase_1")
def run_matching():
    prefix = request.args.get("prefix")
    if not prefix:
        return "Missing prefix", 400

    try:
        input_path = os.path.join(UPLOAD_FOLDER, f"{prefix}_cleaned.csv")
        input_df = pd.read_csv(input_path, delimiter=';', decimal=',', encoding='utf-8')
        gazetteer_df = pd.read_csv(GAZETTEER_FILE, delimiter=';', decimal=',', encoding='utf-8')
    except Exception as e:
        return f"Error loading data: {e}", 500

    patron_saints = load_patron_saints()

    status_file = os.path.join(UPLOAD_FOLDER, f"{prefix}_status.txt")
    processing_path = os.path.join(UPLOAD_FOLDER, f"{prefix}_processing.csv")

    matches = []
    total_rows = len(input_df)

    for loop_counter, (i, row) in enumerate(input_df.iterrows(), start=1):
        # Keep original rowID if present, else fall back to i
        original_row_id = row["rowID"] if "rowID" in row else i

        row_matches = match_row_to_gazetteer(row, gazetteer_df, patron_saints)

        for rm in row_matches:
            if "rowID" not in rm:
                rm["rowID"] = original_row_id
            if "ref_Label" not in rm:
                rm["ref_Label"] = row.get("ref_Label", "")

        matches.extend(row_matches)

        # Progress status using loop_counter for consistent progress
        with open(status_file, "w", encoding="utf-8") as f:
            progress_pct = (loop_counter / total_rows) * 100
            f.write(f"Matching row {loop_counter} of {total_rows} ({progress_pct:.1f}%)\n")

    # Create DataFrame from matches
    if matches:
        result_df = pd.DataFrame(matches)
    else:
        result_df = pd.DataFrame(columns=[
            "rowID", "ref_Label", "gz_id", "toponym-match", "toponym-score",
            "saint-match", "category-match", "territories-match", "phase-1-outcome"
        ])

    # Normalize gz_id column if present and convert to int
    if "gz_id" in result_df.columns:
        result_df["gz_id"] = pd.to_numeric(result_df["gz_id"], errors="coerce").astype("Int64")

    ref_columns = [c for c in input_df.columns if c.startswith("ref_")]

    extra_cols = []
    for col in ["ref_START", "ref_END", "ref_Year", "ref_categoria"]:
        if col in input_df.columns and col not in ref_columns:
            ref_columns.append(col)

    if "rowID" not in input_df.columns:
        input_df["rowID"] = input_df.index

    # Select only the ref_ columns that are NOT already in result_df to avoid duplicates
    ref_columns_to_merge = [c for c in ref_columns if c not in result_df.columns]
    input_context = input_df[["rowID"] + ref_columns_to_merge].copy()

    # Merge with validate to catch any duplicate issues
    processing_df = result_df.merge(input_context, on="rowID", how="left", sort=False, validate="many_to_one")

    manual_columns = {
        "manual_region": pd.NA,
        "manual_province": pd.NA,
        "manual_district": pd.NA,
        "manual_ciudad_villa_only": False
    }

    for col_name, default in manual_columns.items():
        if col_name not in processing_df.columns:
            processing_df[col_name] = default

    # Ensure all ref_columns exist
    for c in ref_columns:
        if c not in processing_df.columns:
            processing_df[c] = pd.NA

    core_order = ["rowID", "ref_Label", "gz_id", "lugar_nombre", "lugar_variantes",
                  "toponym-match", "toponym-score", "saint-match", "category-match",
                  "territories-match", "phase-1-outcome"]
    existing_core = [c for c in core_order if c in processing_df.columns]
    remaining = [c for c in processing_df.columns if c not in existing_core]
   
    ordered = existing_core + [c for c in ref_columns if c in remaining] + \
              [c for c in remaining if c.startswith("manual_")] + \
              [c for c in remaining if c not in ref_columns and not c.startswith("manual_")]

    processing_df = processing_df[ordered]

    try:
        processing_df.to_csv(processing_path, sep=';', index=False, encoding='utf-8')
        # final status update
        with open(status_file, "w", encoding="utf-8") as f:
            f.write(f"Matching complete! Processed {total_rows} rows. Wrote {len(processing_df)} candidate rows to processing CSV.\n")
        logger.info(f"Phase 1 processing saved to {processing_path} ({len(processing_df)} rows).")
    except Exception as e:
        logger.error(f"Error writing processing CSV: {e}")
        with open(status_file, "w", encoding="utf-8") as f:
            f.write(f"Error saving processing CSV: {e}\n")
        return f"Error saving processing file: {e}", 500

    # Debugging info
    logger.debug(f"Gazetteer columns: {list(gazetteer_df.columns)}")
    logger.debug(f"Input columns: {list(input_df.columns)}")
    logger.debug(f"Saved processing file at: {processing_path}")

    flash("Matching completed. Redirecting to progress view...")
    return redirect(url_for(".match_status", prefix=prefix))


@bp.route("/match_status")
def match_status():
    prefix = request.args.get("prefix")
    if not prefix:
        return "Missing prefix", 400

    status_file = os.path.join(UPLOAD_FOLDER, f"{prefix}_status.txt")
    result_file = os.path.join(UPLOAD_FOLDER, f"{prefix}_processing.csv")

    # Check if matching is done
    matching_complete = os.path.exists(result_file)

    try:
        with open(status_file, "r", encoding="utf-8") as f:
            content = f.read()
    except FileNotFoundError:
        content = "Matching not started yet."

    return render_template("match_status.html", prefix=prefix, content=content, matching_complete=matching_complete)


@bp.route("/phase_1b_backoff/<prefix>")
def phase_1b_backoff_route(prefix):
    phase_1b_backoff(prefix)
    flash("Phase 1b (territory backoff) completed.")
    return redirect(url_for(".match_status", prefix=prefix))



# ---------------------------
# Route: Matching Phase 2: Disambiguation
# ---------------------------
@bp.route("/disambiguate/<prefix>", methods=["GET", "POST"])
def disambiguate(prefix):
    input_path = os.path.join(UPLOAD_FOLDER, f"{prefix}_processing.csv")
    if not os.path.exists(input_path):
        flash(f"Missing file: {input_path}")
        return redirect(url_for(".index"))

    if request.method == "POST":
        try:
            user_choices = request.form.to_dict()

            # Load original phase_1 file to update outcomes
            phase1_df = pd.read_csv(input_path, sep=";")
            phase1_df["gz_id"] = pd.to_numeric(phase1_df["gz_id"], errors="coerce").astype("Int64")

            for rowID_str, selected_gz in user_choices.items():
                rowID = int(rowID_str)
                selected_gz = None if selected_gz == "reject" else int(selected_gz)

                is_candidate = (phase1_df["rowID"] == rowID) & (phase1_df["phase-1-outcome"] == "candidate")

                if selected_gz is None:
                    phase1_df.loc[is_candidate, "phase-1-outcome"] = "deleted_candidate"
                else:
                    accepted = is_candidate & (phase1_df["gz_id"] == selected_gz)
                    rejected = is_candidate & (phase1_df["gz_id"] != selected_gz)

                    phase1_df.loc[accepted, "phase-1-outcome"] = "adopted_candidate"
                    phase1_df.loc[rejected, "phase-1-outcome"] = "deleted_candidate"

            phase1_df.to_csv(input_path, sep=";", index=False)
            flash("Disambiguation decisions saved.")
            return redirect(url_for(".index"))

        except Exception as e:
            flash(f"Error processing disambiguation: {e}")
            return redirect(url_for(".index"))

    # GET: run Phase 1b before presenting candidates
    try:
        phase_1b_backoff(prefix)
    except Exception as e:
        logger.error(f"[Disambiguate] Phase 1b failed: {e}")

    # GET: show candidates for review
    try:
        df = pd.read_csv(input_path, sep=";")
        gazetteer_df = pd.read_csv(GAZETTEER_FILE, sep=";", encoding="utf-8")

        # Ensure gz_id is integer type
        df["gz_id"] = pd.to_numeric(df["gz_id"], errors="coerce").astype("Int64")
        gazetteer_df["gz_id"] = pd.to_numeric(gazetteer_df["gz_id"], errors="coerce").astype("Int64")
        
        # Select only columns we need from gazetteer (they already have lugar_ prefix)
        gazetteer_cols = ["gz_id", "lugar_categoria", "lugar_categoria_especial", 
                         "lugar_iglesia_cat", "lugar_partido_generico", 
                         "lugar_provincia_generica", "lugar_region", "Nivel"]
        gazetteer_subset = gazetteer_df[[col for col in gazetteer_cols if col in gazetteer_df.columns]].copy()
        
        # Merge WITHOUT adding prefix (columns already have proper names)
        # Don't include lugar_nombre and lugar_variantes since they're already in df
        merged_df = df.merge(gazetteer_subset, on="gz_id", how="left", suffixes=("", "_gz"))

        from .disambiguation import disambiguate_candidates
        disambig_df = disambiguate_candidates(merged_df)
        grouped = disambig_df.groupby("rowID")

        return render_template("disambiguate.html", grouped=grouped)

    except Exception as e:
        flash(f"Error loading disambiguation data: {e}")
        return redirect(url_for(".index"))

    
# ---------------------------
# Route: Matching Phase 3: Resolve relegated cases
# ---------------------------
@bp.route("/set_constraints/<prefix>", methods=["GET", "POST"])
def set_constraints(prefix):
    entidades_path = os.path.join("data", "gz_entidades.csv")
    df = pd.read_csv(entidades_path, sep=';', encoding='utf-8')
    df.columns = df.columns.str.lower()

    region_choices = sorted(df["reg"].dropna().unique().tolist())

    selected_regions = []
    selected_provinces = []
    selected_category = None

    if request.method == "POST":
        selected_regions = request.form.getlist("regions")
        selected_provinces = request.form.getlist("provinces")
        selected_category = request.form.get("category_filter")

        constraints = {
            "regions": selected_regions,
            "provinces": selected_provinces,
            "category": selected_category
        }

        constraints_path = os.path.join(UPLOAD_FOLDER, f"{prefix}_constraints.json")
        pd.Series(constraints).to_json(constraints_path)

        flash("Global constraints saved. Proceeding to step 2: case-specific territory choice.")
        return redirect(url_for("territory_choice", prefix=prefix))  

    return render_template(
        "phase_3_constraints.html",
        prefix=prefix,
        region_choices=region_choices,
        selected_regions=selected_regions,
        selected_provinces=selected_provinces,
        selected_category=selected_category
    )


@bp.route('/get_provinces/<region_code>')
def get_provinces(region_code):
    entidades_path = os.path.join("data", "gz_entidades.csv")
    try:
        df = pd.read_csv(entidades_path, sep=';', encoding='utf-8')
        provinces = (
            df[df["reg"] == region_code]["provincia_generica"]
            .dropna().unique().tolist()
        )
        provinces.sort()
        return jsonify(provinces)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    
@bp.route("/3_2_territory_choice/<prefix>", methods=["GET", "POST"])
def territory_choice(prefix):
    processing_path = os.path.join(UPLOAD_FOLDER, f"{prefix}_processing.csv")
    if not os.path.exists(processing_path):
        flash(f"Missing processing file: {processing_path}")
        return redirect(url_for(".index"))

    df = pd.read_csv(processing_path, sep=";", encoding="utf-8")

    # Load global constraints
    constraints_path = os.path.join(UPLOAD_FOLDER, f"{prefix}_constraints.json")
    if os.path.exists(constraints_path):
        constraints = pd.read_json(constraints_path, typ="series").to_dict()
    else:
        constraints = {"regions": [], "provinces": [], "category": None}

    entidades_df = load_entidades_csv()

    # Region choices
    if constraints["regions"]:
        region_choices = constraints["regions"]
    else:
        region_choices = sorted(entidades_df["reg"].dropna().unique().tolist())

    # Province choices
    if constraints["provinces"]:
        province_choices = constraints["provinces"]
    elif constraints["regions"]:
        province_choices = sorted(
            entidades_df[entidades_df["reg"].isin(constraints["regions"])]["provincia_generica"]
            .dropna().unique().tolist()
        )
    else:
        province_choices = sorted(entidades_df["provincia_generica"].dropna().unique().tolist())

    # Filter relegated cases
    relegated_df = df[df["phase-1-outcome"] == "relegated"].copy()

    # Prefill from constraints
    single_region = constraints["regions"][0] if len(constraints["regions"]) == 1 else None

    for idx, row in relegated_df.iterrows():
        if pd.isna(row.get("manual_region")) or row["manual_region"] == "":
            if single_region:
                relegated_df.at[idx, "manual_region"] = single_region
            elif constraints["regions"]:
                relegated_df.at[idx, "manual_region"] = constraints["regions"][0]

        if pd.isna(row.get("manual_province")) or row["manual_province"] == "":
            if constraints["provinces"]:
                relegated_df.at[idx, "manual_province"] = constraints["provinces"][0]

        if pd.isna(row.get("manual_district")) or row["manual_district"] == "":
            relegated_df.at[idx, "manual_district"] = "Unknown"

        if pd.isna(row.get("manual_category")) or row["manual_category"] == "":
            if constraints.get("category"):
                relegated_df.at[idx, "manual_category"] = constraints["category"]

    if request.method == "POST":
        form_data = request.form.to_dict()
        for row in relegated_df.itertuples():
            row_id = row.rowID
            df.loc[df["rowID"] == row_id, "manual_region"] = form_data.get(f"region_{row_id}", "")
            df.loc[df["rowID"] == row_id, "manual_province"] = form_data.get(f"province_{row_id}", "")
            df.loc[df["rowID"] == row_id, "manual_district"] = form_data.get(f"district_{row_id}", "")
            df.loc[df["rowID"] == row_id, "manual_category"] = form_data.get(f"category_{row_id}", "")
            df.loc[df["rowID"] == row_id, "manual_ciudad_villa_only"] = (
                f"ciudad_villa_only_{row_id}" in form_data
            )

        df.to_csv(processing_path, sep=";", index=False, encoding="utf-8")
        flash("Manual territory assignments saved.")
        return redirect(url_for(".index"))

    return render_template(
        "phase_3_territory_choice.html",
        prefix=prefix,
        relegated_cases=relegated_df.to_dict(orient="records"),
        region_choices=region_choices,
        province_choices=province_choices
    )



@bp.route('/get_districts/<province>')
def get_districts(province):
    df = load_entidades_csv()
    districts = sorted(
        df[df["provincia_generica"] == province]["partido_generico"]
        .dropna().unique().tolist()
    )
    if "Unknown" not in districts:
        districts.insert(0, "Unknown")
    return jsonify(districts)


