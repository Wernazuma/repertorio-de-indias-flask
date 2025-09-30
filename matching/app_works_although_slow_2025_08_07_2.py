import os, threading, time, re, logging, traceback
import pandas as pd
from flask import Flask, request, render_template, redirect, url_for, flash, jsonify
from collections import defaultdict
from fuzzywuzzy import fuzz
from disambiguation import disambiguate_candidates
from logging.handlers import RotatingFileHandler

# Setup logging to file
log_folder = os.path.join("..", "data", "logs")
os.makedirs(log_folder, exist_ok=True)

log_file_path = os.path.join(log_folder, "matching.log")

file_handler = RotatingFileHandler(log_file_path, maxBytes=5_000_000, backupCount=3)
file_handler.setLevel(logging.DEBUG)
formatter = logging.Formatter('%(asctime)s %(levelname)s: %(message)s')
file_handler.setFormatter(formatter)

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)
logger.addHandler(file_handler)



app = Flask(__name__)
app.secret_key = 'your_secret_key'

UPLOAD_FOLDER = os.path.join("..", "data", "uploads")
GAZETTEER_FILE = os.path.join("..", "data", "espartede.csv")
PATRON_FILE = os.path.join("..", "data", "santos.csv")




def load_entidades_csv():
    path = os.path.join("..", "data", "gz_entidades.csv")
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

    # --- Saint match ---
    saint_match = "saint_null"
    for saint in patron_saints:
        if saint.lower() in ref_label.lower():
            if pd.notna(row.get("lugar_santo")) and saint.lower() in str(row["lugar_santo"]).lower():
                saint_match = "saint_match"
            else:
                saint_match = "saint_mismatch"
            break

    # --- Category match ---
    ref_cat = input_row.get("ref_categoria")
    if ref_cat:
        cat_match = (
            "category_match"
            if ref_cat in [row.get("lugar_categoria"), row.get("lugar_categoria_especial")]
            else "category_mismatch"
        )
    else:
        cat_match = "category_null"

    # --- Core result structure ---
    result = {
        "rowID": input_row.get("rowID"),
        "ref_Label": ref_label,
        "gz_id": row.get("gz_id"),
        "lugar_nombre": row.get("lugar_nombre",""),
        "lugar_variantes": row.get("lugar_variantes",""),
        "toponym-match": toponym_match,
        "toponym-score": score,
        "saint-match": saint_match,
        "category-match": cat_match,
        "territories-match": "matched",
        "phase-1-outcome": "candidate"
    }

    # --- Add all other ref_ fields dynamically ---
    for key in input_row.index:
        if key.startswith("ref_") and key not in result:
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
                return [result]
            results.append(result)
        elif clean_toponym(row["lugar_nombre"]) == cleaned_label:
            result = create_match_result(input_row, row, "toponym_nombre", 1, patron_saints)
            if is_high_confidence(result):
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
                            return [result]
                        results.append(result)
                        break  # Only keep the best fuzzy match per row
    
    return results


# ---------------------------
# Matching Logic
# ---------------------------
def match_row_to_gazetteer(input_row, gazetteer_df, patron_saints):
    results = []

    ref_label = input_row.get("ref_Label", "")
    cleaned_label = clean_toponym(ref_label)
    logger.debug(f"[{input_row.get('rowID')}] ref_Label: {ref_label} → cleaned: {cleaned_label}")

    # Check for minimal spatial reference before proceeding
    required_fields = [
        "ref_Partido_generico", "ref_Partido", "ref_Jurisdiccion",
        "ref_Provincia", "ref_Provincia_menor", "ref_Provincia_mayor"
    ]

    has_spatial_info = any(
        pd.notna(input_row.get(f)) and str(input_row.get(f)).strip() != ""
        for f in required_fields
    )

    if not has_spatial_info:
        logger.warning(f"[{input_row.get('rowID')}] No usable spatial fields. Relegating early.")
        return [{
            "rowID": input_row["rowID"],
            "ref_Label": input_row.get("ref_Label", ""),
            "gz_id": None,
            "toponym-match": "null",
            "toponym-score": 0,
            "saint-match": "null",
            "category-match": "null",
            "territories-match": "missing_spatial_fields",
            "phase-1-outcome": "relegated"
        }]




    filtered_df = gazetteer_df.copy()


    # Basic filtering (territory generic)
    territory_fields_generic = ["ref_Partido_generico", "ref_Provincia_generica", "ref_Region", "ref_Pais"]
    for field in territory_fields_generic:
        value = input_row.get(field, None)
        if pd.notna(value) and value != '':
            col = field.replace("ref_", "lugar_").lower()
            
            if field == "ref_Region":
                value_mapped = REGION_CODE_TO_NAME.get(value)
                if value_mapped:
                    logger.debug(f"[{input_row.get('rowID')}] Mapping region code {value} → {value_mapped}")
                    value = value_mapped
                else:
                    logger.warning(f"[{input_row.get('rowID')}] Unknown region code: {value}")
                    continue  # Skip region filter if code not found

            if col in filtered_df.columns:
                logger.debug(f"[{input_row.get('rowID')}] Filtering by {col} = {value}")
                filtered_df = filtered_df[filtered_df[col] == value]
            else:
                logger.warning(f"[{input_row.get('rowID')}] Field {col} not found in gazetteer")


    logger.debug(f"[{input_row.get('rowID')}] After generic territory filter: {len(filtered_df)} rows")
    
    # Time filtering
    year = None
    if pd.notna(input_row.get("ref_Year")):
        year = int(input_row["ref_Year"])
    elif pd.notna(input_row.get("ref_START")) and pd.notna(input_row.get("ref_END")):
        year = (int(input_row["ref_START"]) + int(input_row["ref_END"])) // 2

    if year:
        year = min(max(year, 1701), 1808)
        logger.debug(f"[{input_row.get('rowID')}] Time filter using year: {year}")
        if 'overlap_start' in filtered_df.columns and 'overlap_end' in filtered_df.columns:
            filtered_df = filtered_df[
                (filtered_df['overlap_start'] <= year) & (filtered_df['overlap_end'] >= year)
            ]
        else:
            logger.warning(f"[{input_row.get('rowID')}] Temporal columns missing in gazetteer")

    logger.debug(f"[{input_row.get('rowID')}] After time filter: {len(filtered_df)} rows")


    # === Complex territorial filtering ===
    territory_field_map = {
        "ref_Partido": "Partido",
        "ref_Jurisdiccion": "Jurisdiccion",
        "ref_Provincia": "Provincia",
        "ref_Provincia_menor": "Provincia menor",
        "ref_Provincia_mayor": "Provincia mayor",
        "ref_Obispado": "Obispado",
        "ref_Audiencia": "Audiencia"
    }

    for field, expected_nivel in territory_field_map.items():
        value = input_row.get(field, None)
        if pd.notna(value) and value != '':
            cleaned_value = clean_toponym(value)
            logger.debug(f"[{input_row.get('rowID')}] Filtering for {field}: {value} → {cleaned_value} with Nivel={expected_nivel}")

            def territory_match(row):
                if row.get("Nivel") != expected_nivel:
                    return False
                for col in ["polygon_label", "polygon_nombre"]:
                    val = row.get(col)
                    if pd.notna(val) and fuzz.ratio(clean_toponym(val), cleaned_value) >= 85:
                        return True
                if pd.notna(row.get("polygon_variantes")):
                    variants = [clean_toponym(v.strip()) for v in str(row["polygon_variantes"]).split(" \\ ")]
                    for variant in variants:
                        if fuzz.ratio(variant, cleaned_value) >= 85:
                            return True
                return False

            before = len(filtered_df)
            filtered_df = filtered_df[filtered_df.apply(territory_match, axis=1)]
            after = len(filtered_df)
            logger.debug(f"[{input_row.get('rowID')}] After {field} + Nivel='{expected_nivel}' filter: {after} rows (was {before})")

    if filtered_df.empty:
        logger.warning(f"[{input_row.get('rowID')}] No candidates after territory filtering.")
        return [{
            "rowID": input_row["rowID"],
            "ref_Label": input_row.get("ref_Label", ""),
            "gz_id": None,
            "toponym-match": "null",
            "toponym-score": 0,
            "saint-match": "null",
            "category-match": "null",
            "territories-match": "no_candidates",
            "phase-1-outcome": "relegated"
        }]




    # Toponym matching
    for idx, row in filtered_df.iterrows():
        toponym_match = None
        score = 0

        logger.debug(f"[{input_row.get('rowID')}] Comparing with gazetteer row {row.get('gz_id')}")

        # Your existing toponym matching logic here...
        if clean_toponym(row["lugar_label"]) == cleaned_label:
            toponym_match = "toponym_label"
            score = 1
        elif clean_toponym(row["lugar_nombre"]) == cleaned_label:
            toponym_match = "toponym_nombre"
            score = 1
        elif pd.notna(row.get("lugar_variantes")):
            variants = [clean_toponym(v.strip()) for v in row["lugar_variantes"].split(" \\ ")]
            if cleaned_label in variants:
                toponym_match = "toponym_variante"
                score = 1
            else:
                best_score = 0
                for variant in variants:
                    lev_score = fuzz.ratio(cleaned_label, variant) / 100
                    logger.debug(f"[{input_row.get('rowID')}] Variant: {variant} → Score: {lev_score}")
                    if lev_score >= 0.85 and lev_score > best_score:
                        best_score = lev_score
                        toponym_match = "toponym_levenshtein"
                        score = best_score
                if not toponym_match:
                    for lw in cleaned_label.split():
                        if lw in variants:
                            toponym_match = "toponym_partial"
                            score = 1
                            break

        if toponym_match:
            logger.info(f"[{input_row.get('rowID')}] Match found: {toponym_match} (score: {score})")
            
            # Saint match
            saint_match = "saint_null"
            for saint in patron_saints:
                if saint.lower() in ref_label.lower():
                    if pd.notna(row.get("lugar_santo")) and saint.lower() in str(row["lugar_santo"]).lower():
                        saint_match = "saint_match"
                    else:
                        saint_match = "saint_mismatch"
                    break

            # Category match
            ref_cat = input_row.get("ref_categoria")
            if ref_cat:
                cat_match = (
                    "category_match" if ref_cat in [row.get("lugar_categoria"), row.get("lugar_categoria_especial")]
                    else "category_mismatch"
                )
            else:
                cat_match = "category_null"

            result = {
                "rowID": input_row["rowID"],
                "ref_Label": ref_label,
                "gz_id": row["gz_id"],
                "toponym-match": toponym_match,
                "toponym-score": score,
                "saint-match": saint_match,
                "category-match": cat_match,
                "territories-match": "matched",
                "phase-1-outcome": "candidate"
            }

            # EARLY EXIT: Check for high confidence match
            if (toponym_match in ["toponym_nombre", "toponym_label"] and 
                saint_match in ["saint_match", "saint_null"] and 
                cat_match in ["category_match", "category_null"]):
                logger.info(f"[{input_row.get('rowID')}] High confidence match — stopping further checks.")
                result["phase-1-outcome"] = "auto_adopt"
                return [result]  # Return immediately with just this one result
            
            results.append(result)

    # Handle case where no matches found
    if not results:
        logger.warning(f"[{input_row.get('rowID')}] No match found.")
        return [{
            "rowID": input_row["rowID"],
            "ref_Label": input_row.get("ref_Label", ""),
            "gz_id": None,
            "toponym-match": "null",
            "toponym-score": 0,
            "saint-match": "null",
            "category-match": "null",
            "territories-match": "no_matches",
            "phase-1-outcome": "relegated"
        }]

    return results


# ---------------------------
# Route: Upload + detection
# ---------------------------
@app.route("/", methods=["GET", "POST"])
def upload_form():
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
# Route: Matching Phase 1
# ---------------------------
@app.route("/match_phase_1")
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

    matches = []
    total_rows = len(input_df)

    for i, row in input_df.iterrows():
        row["rowID"] = i
        row_matches = match_row_to_gazetteer(row, gazetteer_df, patron_saints)

        matches.extend(row_matches)

        # Update status
        with open(status_file, "w", encoding="utf-8") as f:
            f.write(f"Matching row {i + 1} of {total_rows}...\n")


    result_df = pd.DataFrame(matches)
    if "gz_id" in result_df.columns:
        result_df["gz_id"] = pd.to_numeric(result_df["gz_id"], errors="coerce").dropna().astype(int)
    result_path = os.path.join(UPLOAD_FOLDER, f"{prefix}csv")
    result_df.to_csv(result_path, sep=';', index=False)

    logger.debug(f"Gazetteer columns: {list(gazetteer_df.columns)}")
    logger.debug(f"Input columns: {list(input_df.columns)}")

    flash("Matching started. Redirecting to progress view...")
    return redirect(url_for("match_status", prefix=prefix))

@app.route("/match_status")
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

# ---------------------------
# Route: Matching Phase 2: Disambiguation
# ---------------------------


@app.route("/disambiguate/<prefix>", methods=["GET", "POST"])
def disambiguate(prefix):
    input_path = os.path.join(UPLOAD_FOLDER, f"{prefix}_processing.csv")
    if not os.path.exists(input_path):
        flash(f"Missing file: {input_path}")
        return redirect(url_for("upload_form"))

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
            return redirect(url_for("upload_form"))

        except Exception as e:
            flash(f"Error processing disambiguation: {e}")
            return redirect(url_for("upload_form"))

    # GET: show candidates for review
    try:
        df = pd.read_csv(input_path, sep=";")
        gazetteer_df = pd.read_csv(GAZETTEER_FILE, sep=";", encoding="utf-8")

        # Merge for display
        df["gz_id"] = pd.to_numeric(df["gz_id"], errors="coerce").astype("Int64")
        gazetteer_df["gz_id"] = pd.to_numeric(gazetteer_df["gz_id"], errors="coerce").astype("Int64")
        # After merging gazetteer with lugar_ prefix
        merged_df = df.merge(
            gazetteer_df.add_prefix("lugar_"),
            left_on="gz_id",
            right_on="lugar_gz_id",
            how="left"
        )

        # Rename lugar_lugar_* → lugar_*
        merged_df.columns = [
            col.replace("lugar_lugar_", "lugar_") if col.startswith("lugar_lugar_") else col
            for col in merged_df.columns
        ]


        from disambiguation import disambiguate_candidates
        disambig_df = disambiguate_candidates(merged_df)
        grouped = disambig_df.groupby("rowID")

        return render_template("disambiguate.html", grouped=grouped)

    except Exception as e:
        flash(f"Error loading disambiguation data: {e}")
        return redirect(url_for("upload_form"))

# ---------------------------
# Route: Matching Phase 3: Resolve relegated cases
# ---------------------------
@app.route("/set_constraints/<prefix>", methods=["GET", "POST"])
def set_constraints(prefix):
    entidades_path = os.path.join("..", "data", "gz_entidades.csv")
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


@app.route('/get_provinces/<region_code>')
def get_provinces(region_code):
    entidades_path = os.path.join("..", "data", "gz_entidades.csv")
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

@app.route("/3_2_territory_choice/<prefix>", methods=["GET", "POST"])
def territory_choice(prefix):
    entidades_df = load_entidades_csv()

    # Load global constraints
    constraints_path = os.path.join(UPLOAD_FOLDER, f"{prefix}_constraints.json")
    if os.path.exists(constraints_path):
        constraints = pd.read_json(constraints_path, typ="series").to_dict()
    else:
        constraints = {"regions": [], "provinces": [], "category": None}

    # Region list: restricted or full
    if constraints["regions"]:
        region_choices = constraints["regions"]
    else:
        region_choices = sorted(entidades_df["reg"].dropna().unique().tolist())

    # Province list: restricted or full
    if constraints["provinces"]:
        province_choices = constraints["provinces"]
    elif constraints["regions"]:
        province_choices = sorted(
            entidades_df[entidades_df["reg"].isin(constraints["regions"])]
            ["provincia_generica"].dropna().unique().tolist()
        )
    else:
        province_choices = sorted(entidades_df["provincia_generica"].dropna().unique().tolist())

    # Load phase 1 results and filter relegated
    phase1_path = os.path.join(UPLOAD_FOLDER, f"{prefix}_processing.csv")
    if not os.path.exists(phase1_path):
        flash("Phase 1 results not found.")
        return redirect(url_for("set_constraints", prefix=prefix))

    phase1_df = pd.read_csv(phase1_path, sep=";", encoding="utf-8")
    relegated_df = phase1_df[phase1_df["phase-1-outcome"] == "relegated"].copy()

    if request.method == "POST":
        case_constraints = []
        for _, row in relegated_df.iterrows():
            rid = str(row["rowID"])
            selected_region = request.form.get(f"region_{rid}")
            selected_province = request.form.get(f"province_{rid}")
            selected_district = request.form.get(f"district_{rid}")
            ciudad_villa_only = request.form.get(f"ciudad_villa_only_{rid}") == "on"

            case_constraints.append({
                "rowID": rid,
                "region": selected_region,
                "province": selected_province,
                "district": None if selected_district == "Unknown" else selected_district,
                "ciudad_villa_only": ciudad_villa_only
            })

        # TODO: Save case_constraints somewhere for later phase 3 processing

        flash("Case-specific constraints saved for all relegated cases.")
        return redirect(url_for("some_next_step", prefix=prefix))

    return render_template(
        "phase_3_territory_choice.html",
        prefix=prefix,
        relegated_cases=relegated_df.to_dict(orient="records"),
        region_choices=region_choices,
        province_choices=province_choices,
        category_constraint=constraints.get("category", None)
    )

@app.route('/get_districts/<province>')
def get_districts(province):
    df = load_entidades_csv()
    districts = sorted(
        df[df["provincia_generica"] == province]["partido_generico"]
        .dropna().unique().tolist()
    )
    if "Unknown" not in districts:
        districts.insert(0, "Unknown")
    return jsonify(districts)


if __name__ == "__main__":
    app.run(debug=True)
