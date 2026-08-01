from flask import Flask, render_template, request, jsonify, session, redirect, url_for, send_file, current_app
import os
import pandas as pd
import numpy as np
import csv
import re
from werkzeug.utils import secure_filename
from dateutil.parser import parse
import tempfile
from datetime import datetime
from . import bp

#app = Flask(__name__, template_folder=os.path.join(os.path.dirname(__file__), 'templates'))
#app.secret_key = 'your-secret-key-here'  # Change this to a random secret key


ALLOWED_EXTENSIONS = {'csv'}  


def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def is_possibly_number(val):
    try:
        float(val)
        return True
    except:
        return False

def is_possibly_date(val):
    try:
        parse(val, fuzzy=False)
        return True
    except:
        return False

def classify_column_type(samples):
    if all(is_possibly_number(x) for x in samples if pd.notnull(x)):
        return "number"
    if all(is_possibly_date(x) for x in samples if pd.notnull(x)):
        return "date"
    return "string"

def _suggest_type(samples):
    """Suggest a data type for the classify-columns radios (integer/decimal), or
    None when it isn't obviously numeric."""
    vals = [str(s).strip() for s in samples
            if s is not None and str(s).strip() and str(s).strip().lower() != "nan"]
    if not vals:
        return None
    if all(re.fullmatch(r"-?\d+", v) for v in vals):
        return "integer"
    if all(re.fullmatch(r"-?\d+(?:[.,]\d+)?", v) for v in vals):
        return "decimal"
    return None

def detect_csv_format(file_path):
    """Detect CSV format (separator, text qualifier, decimal marker)"""
    with open(file_path, 'r', encoding='utf-8') as f:
        sample_lines = []
        for i, line in enumerate(f):
            if i < 10:
                sample_lines.append(line.strip())
            else:
                break
    
    sample_text = '\n'.join(sample_lines)
    sniffer = csv.Sniffer()
    
    try:
        dialect = sniffer.sniff(sample_text, delimiters=',;\t|')
        detected_sep = dialect.delimiter
    except:
        separators = {',': 0, ';': 0, '\t': 0, '|': 0}
        for line in sample_lines:
            for sep in separators:
                separators[sep] += line.count(sep)
        detected_sep = max(separators, key=separators.get)
    
    text_qualifier = None
    if '"' in sample_text:
        text_qualifier = '"'
    elif "'" in sample_text:
        text_qualifier = "'"
    
    decimal_marker = '.'
    for line in sample_lines:
        if re.search(r'\d+,\d+', line):
            decimal_marker = ','
            break
        elif re.search(r'\d+\.\d+', line):
            decimal_marker = '.'
            break
    
    return detected_sep, text_qualifier, decimal_marker

def convert_to_expected_format(file_path, detected_sep, text_qualifier, decimal_marker):
    """Convert CSV to expected format (separator=';', decimal='.')"""
    expected_sep = ';'
    expected_decimal = '.'
    
    if detected_sep == expected_sep and decimal_marker == expected_decimal:
        return file_path
    
    read_kwargs = {'sep': detected_sep, 'encoding': 'utf-8'}
    if text_qualifier:
        read_kwargs['quotechar'] = text_qualifier
    
    df = pd.read_csv(file_path, **read_kwargs)
    
    if decimal_marker == ',' and expected_decimal == '.':
        for col in df.columns:
            if df[col].dtype == 'object':
                df[col] = df[col].astype(str).str.replace(',', '.', regex=False)
    
    temp_path = file_path.replace('.csv', '_formatted.csv')
    df.to_csv(temp_path, sep=expected_sep, index=False, encoding='utf-8')
    
    return temp_path

def detect_and_classify_columns(df):
    """Detect and classify columns, return field types and samples"""
    field_types = {}
    field_samples = {}
    
    for field in df.columns:
        sample = df[field].dropna().astype(str).head(5).tolist()
        suggestion = classify_column_type(sample)
        field_samples[field] = sample
        
        if suggestion in ["number", "date"]:
            field_types[field] = "needs_classification"
        else:
            field_types[field] = "text"
    
    return field_types, field_samples

def _dedupe_columns(df):
    """Give duplicate column labels a suffix.

    Field mapping and the chronology step rename columns, which can collide with a
    header the uploaded table already has (e.g. a source column literally called
    'ref_Label', or an existing 'ref_END'). With a duplicated label, df[col] returns
    a DataFrame instead of a Series and the coercion below fails with
    "'DataFrame' object has no attribute 'str'". Keep the first, suffix the rest so
    no data is silently lost.
    """
    seen, out = {}, []
    for col in df.columns:
        if col in seen:
            seen[col] += 1
            out.append(f"{col}_dup{seen[col]}")
        else:
            seen[col] = 0
            out.append(col)
    if out != list(df.columns):
        print(f"Renamed duplicate columns: {set(out) - set(df.columns)}")
        df.columns = out
    return df


def clean_and_coerce_data(df, field_types):
    """Clean and coerce data based on field types"""
    df = _dedupe_columns(df.copy())
    cleaned_df = df.copy()
    legacy_columns = {}

    # Only process columns that actually exist in the DataFrame
    available_columns = set(df.columns)
    
    print(f"=== DEBUGGING CLEAN_AND_COERCE_DATA ===")
    print(f"DataFrame columns: {list(df.columns)}")
    print(f"Field types keys: {list(field_types.keys())}")
    
    for col, dtype in field_types.items():
        # Skip columns that don't exist in the DataFrame
        if col not in available_columns:
            print(f"Skipping column '{col}' - not found in DataFrame")
            continue
            
        print(f"Processing column '{col}' as type '{dtype}'")
        
        if dtype in ["integer", "decimal"]:
            cleaned_df[col] = cleaned_df[col].astype(str).str.replace(",", ".", regex=False)

        try:
            if dtype == "integer":
                cleaned_df[col] = pd.to_numeric(cleaned_df[col], errors="coerce").astype("Int64")
            elif dtype == "decimal":
                cleaned_df[col] = pd.to_numeric(cleaned_df[col], errors="coerce")
            elif dtype == "date":
                cleaned_df[col] = pd.to_datetime(cleaned_df[col], errors="coerce", dayfirst=True)
            elif dtype == "ignore":
                cleaned_df.drop(columns=[col], inplace=True)
                continue  # Skip legacy column creation for ignored columns
        except Exception as e:
            print(f"Error coercing field {col} to {dtype}: {e}")

        # Create legacy columns for converted data
        if dtype in ["integer", "decimal", "date"]:
            legacy_col = f"legacy_{col}"
            mask = cleaned_df[col].isna() & df[col].notna()
            legacy_data = df[col].where(mask)
            if legacy_data.notna().any():
                cleaned_df[legacy_col] = legacy_data
                legacy_columns[col] = legacy_col

    return cleaned_df, legacy_columns

@bp.route('/')
def upload_csv():
    # Clear session data for new upload
    discarded = request.args.get('discarded', type=int)
    session.clear()
    return render_template('upload_csv.html', discarded=discarded)

def _base_taken(base, folder):
    """A table name is taken if the raw upload or its cleaned output exists."""
    return (os.path.exists(os.path.join(folder, f"{base}.csv"))
            or os.path.exists(os.path.join(folder, f"{base}_cleaned.csv")))


def _next_free_base(base, folder):
    n = 1
    while _base_taken(f"{base}_{n}", folder):
        n += 1
    return f"{base}_{n}"


# ---------------------------------------------------------------------------
# Artifacts owned by the transform step, for abort / delete-my-table.
# ---------------------------------------------------------------------------
TRANSFORM_ARTIFACTS = ("{base}.csv", "{base}_formatted.csv", "{base}_cleaned.csv",
                       "{base}_domain.txt", "{base}_metadata.txt",
                       "{base}_bibliography.txt", "{base}_field_definitions.csv")


def _current_base():
    """The dataset prefix for the session, or '' when nothing is uploaded."""
    base = session.get('prefix')
    if not base and session.get('uploaded_file'):
        base = os.path.splitext(session['uploaded_file'])[0]
    return base or ''


def _metadata_exists(base):
    return bool(base) and os.path.exists(
        os.path.join(current_app.config['UPLOAD_FOLDER'], f"{base}_metadata.txt"))


def _delete_artifacts(base):
    """Remove the raw upload and everything derived from it. Returns the names removed."""
    folder = current_app.config['UPLOAD_FOLDER']
    removed = []
    for pattern in TRANSFORM_ARTIFACTS:
        path = os.path.join(folder, pattern.format(base=base))
        try:
            if os.path.exists(path):
                os.remove(path)
                removed.append(os.path.basename(path))
        except OSError:
            pass
    return removed


@bp.route('/discard', methods=['POST'])
def discard():
    """Abort the workflow: delete the uploaded table and all derived files.

    Accepts an explicit ?prefix / form prefix so it also works after matching or
    on a prefix-only resume (no active session)."""
    base = request.values.get('prefix', '').strip() or _current_base()
    removed = _delete_artifacts(base) if base else []
    session.clear()
    return redirect(url_for('.upload_csv', discarded=len(removed)))


@bp.route('/upload', methods=['POST'])
def upload_file():
    if 'file' not in request.files:
        return jsonify({'error': 'No file selected'}), 400

    file = request.files['file']
    if file.filename == '':
        return jsonify({'error': 'No file selected'}), 400

    if not (file and allowed_file(file.filename)):
        return jsonify({'error': 'Invalid file type. Please upload a CSV file.'}), 400

    folder = current_app.config['UPLOAD_FOLDER']
    os.makedirs(folder, exist_ok=True)

    # Desired name (chosen by the user after a conflict warning), else the upload name.
    desired = (request.form.get('desired_name') or '').strip()
    src = desired if desired else file.filename
    base = os.path.splitext(secure_filename(src))[0]
    if not base:
        return jsonify({'error': 'Invalid file name.'}), 400

    # Conflict: warn instead of silently renaming, proposing the next free name.
    if _base_taken(base, folder):
        return jsonify({'conflict': True, 'existing': base,
                        'proposed': _next_free_base(base, folder)})

    filename = f"{base}.csv"
    file.save(os.path.join(folder, filename))

    session['uploaded_file'] = filename
    session['original_filename'] = file.filename
    session['prefix'] = base
    return jsonify({'success': True, 'filename': filename})

@bp.route('/select_domain')
def select_domain():
    if 'uploaded_file' not in session:
        return redirect(url_for('.upload_csv'))
    return render_template('select_domain.html')

@bp.route('/process_domain', methods=['POST'])
def process_domain():
    if 'uploaded_file' not in session:
        return jsonify({'error': 'No file uploaded'}), 400
    
    domain = request.json.get('domain')
    if domain not in ['places', 'territories']:
        return jsonify({'error': 'Invalid domain selection'}), 400

    session['domain'] = domain
    # Persist per-prefix so the matching step can pick up the domain on resume
    # (users aren't logged in — the prefix is the only handle).
    base = session.get('prefix') or os.path.splitext(session['uploaded_file'])[0]
    try:
        with open(os.path.join(current_app.config['UPLOAD_FOLDER'], f"{base}_domain.txt"),
                  'w', encoding='utf-8') as _df:
            _df.write(domain)
    except OSError:
        pass

    # Process the CSV file
    filepath = os.path.join(current_app.config['UPLOAD_FOLDER'], session['uploaded_file'])
    
    try:
        # Detect and convert CSV format
        detected_sep, text_qualifier, decimal_marker = detect_csv_format(filepath)
        formatted_path = convert_to_expected_format(filepath, detected_sep, text_qualifier, decimal_marker)
        
        # Load the formatted CSV
        df = pd.read_csv(formatted_path, sep=";", encoding='utf-8')
        
        # Store CSV info in session
        session['csv_rows'] = len(df)
        session['csv_columns'] = len(df.columns)
        session['formatted_path'] = formatted_path
        
        # Detect and classify columns
        field_types, field_samples = detect_and_classify_columns(df)
        
        # Store column info in session
        session['field_types'] = field_types
        session['field_samples'] = field_samples
        session['columns'] = df.columns.tolist()
        
        return jsonify({
            'success': True,
            'rows': len(df),
            'columns': len(df.columns),
            'next_step': 'classify_columns'
        })
        
    except Exception as e:
        return jsonify({'error': f'Error processing CSV: {str(e)}'}), 500

@bp.route('/classify_columns')
def classify_columns():
    if 'field_types' not in session:
        return redirect(url_for('.upload_csv'))
    
    # Debug: Let's see what field types we have
    print("=== DEBUGGING CLASSIFY COLUMNS ===")
    print(f"Field types in session: {session['field_types']}")
    print(f"Field samples in session: {session['field_samples']}")
    
    # Get columns that need classification
    needs_classification = {col: session['field_samples'][col] 
                          for col, ftype in session['field_types'].items() 
                          if ftype == 'needs_classification'}
    
    print(f"Columns that need classification: {needs_classification}")
    
    if not needs_classification:
        # No columns need classification, go to field mapping
        print("No columns need classification, redirecting to field mapping")
        return redirect(url_for('.field_mapping'))

    # Suggest a type per column so the radios come pre-selected sensibly.
    suggested = {col: _suggest_type(samples) for col, samples in needs_classification.items()}

    return render_template('classify_columns.html',
                         columns=needs_classification,
                         suggested=suggested)

@bp.route('/save_column_types', methods=['POST'])
def save_column_types():
    if 'field_types' not in session:
        return jsonify({'error': 'No field types in session'}), 400
    
    column_types = request.json.get('column_types', {})
    
    # Store debug info
    debug_info = {
        'original_field_types': session['field_types'].copy(),
        'received_column_types': column_types
    }
    
    # Update field types with user selections
    for col, col_type in column_types.items():
        session['field_types'][col] = col_type
    
    debug_info['updated_field_types'] = session['field_types'].copy()
    
    # Store debug info in session so we can see it later
    session['debug_column_types'] = debug_info
    
    return jsonify({'success': True, 'debug': debug_info})

@bp.route('/field_mapping')
def field_mapping():
    if 'field_types' not in session or 'domain' not in session:
        return redirect(url_for('.upload_csv'))
    
    domain = session['domain']
    columns = session['columns']
    field_types = session['field_types']
    
    # Get columns by type for filtering
    text_columns = [col for col, ftype in field_types.items() if ftype in ['text', 'string']]
    integer_columns = [col for col, ftype in field_types.items() if ftype == 'integer']
    date_columns = [col for col, ftype in field_types.items() if ftype == 'date']
    
    # Define required fields based on domain
    if domain == 'places':
        required_fields = [
            {'name': 'ref_Label', 'label': 'Associated place\'s name', 'types': ['text', 'string'], 'optional': False},
            {'name': 'rowID', 'label': 'Internal ID of each row', 'types': ['integer', 'text', 'string'], 'optional': True},
            {'name': 'ref_Variantes', 'label': 'Variant or variants of the name', 'types': ['text', 'string'], 'optional': True},
            {'name': 'ref_categoria', 'label': 'Settlement type / category (ciudad, villa, pueblo, curato…)', 'types': ['text', 'string'], 'optional': True},
            {'name': 'ref_Partido', 'label': 'District/partido (including corregimientos de naturales)', 'types': ['text', 'string'], 'optional': True},
            {'name': 'ref_Jurisdiccion', 'label': 'City jurisdiction or minor province', 'types': ['text', 'string'], 'optional': True},
            {'name': 'ref_Provincia', 'label': 'Province (Gobierno, Intendencia)', 'types': ['text', 'string'], 'optional': True},
            {'name': 'ref_Obispado', 'label': 'Bishopric', 'types': ['text', 'string'], 'optional': True},
            {'name': 'ref_Audiencia', 'label': 'Audiencia real', 'types': ['text', 'string'], 'optional': True},
            {'name': 'ref_Pais', 'label': 'Modern country (ISO 3166-1 alpha-3)', 'types': ['text', 'string'], 'optional': True},
            {'name': 'ref_Region', 'label': 'Generic region', 'types': ['text', 'string'], 'optional': True},
            {'name': 'ref_Provincia_generica', 'label': 'Generic province', 'types': ['text', 'string'], 'optional': True},
        ]
    else:  # territories
        required_fields = [
            {'name': 'ref_Label', 'label': 'Territory\'s name', 'types': ['text', 'string'], 'optional': False},
            {'name': 'rowID', 'label': 'Internal ID of each row', 'types': ['integer', 'text', 'string'], 'optional': True},
            {'name': 'ref_Variantes', 'label': 'Variant or variants of the name', 'types': ['text', 'string'], 'optional': True},
            {'name': 'ref_Titulo', 'label': 'Territory title (alcaldía mayor, corregimiento, etc.)', 'types': ['text', 'string'], 'optional': True},
            {'name': 'ref_Nivel', 'label': 'Hierarchical level or general type', 'types': ['text', 'string'], 'optional': True},
            {'name': 'ref_Provincia_generica', 'label': 'Generic province it is in', 'types': ['text', 'string'], 'optional': True},
            {'name': 'ref_Region', 'label': 'Generic region it is in', 'types': ['text', 'string'], 'optional': True},
        ]
    
    # Filter columns by allowed types for each field
    filtered_columns = {}
    for field in required_fields:
        allowed_cols = [col for col, ftype in field_types.items() 
                       if ftype in field['types']]
        filtered_columns[field['name']] = allowed_cols
    
    return render_template('field_mapping.html', 
                         required_fields=required_fields,
                         columns=columns,
                         filtered_columns=filtered_columns,
                         domain=domain)

@bp.route('/chronological_info')
def chronological_info():
    if 'field_mapping' not in session:
        return redirect(url_for('.upload_csv'))
    
    field_types = session['field_types']
    
    # Debug: Let's see what we have in field_types
    print("=== DEBUGGING CHRONOLOGICAL INFO ===")
    print(f"Field types: {field_types}")
    
    # Get integer columns
    integer_columns = [col for col, ftype in field_types.items() if ftype == 'integer']
    print(f"Integer columns found: {integer_columns}")
    
    # Also check if there are any columns at all
    all_columns = list(field_types.keys())
    print(f"All columns: {all_columns}")
    
    # Get debug info from previous step
    debug_column_types = session.get('debug_column_types', None)
    
    return render_template('chronological_info.html', 
                         integer_columns=integer_columns,
                         debug_column_types=debug_column_types)

@bp.route('/save_field_mapping', methods=['POST'])
def save_field_mapping():
    if 'field_types' not in session:
        return jsonify({'error': 'No field types in session'}), 400
    
    field_mapping = request.json.get('field_mapping', {})
    session['field_mapping'] = field_mapping
    
    return jsonify({'success': True})

@bp.route('/save_chronological_info', methods=['POST'])
def save_chronological_info():
    chronological_info = request.json
    session['chronological_info'] = chronological_info
    
    return jsonify({'success': True})

@bp.route('/process_final')
def process_final():
    if 'chronological_info' not in session:
        return redirect(url_for('.upload_csv'))
    
    try:
        print("=== DEBUGGING PROCESS_FINAL ===")
        
        # Load the CSV
        filepath = session['formatted_path']
        print(f"Loading CSV from: {filepath}")
        df = pd.read_csv(filepath, sep=";", encoding='utf-8')
        print(f"Original CSV shape: {df.shape}")
        print(f"Original columns: {df.columns.tolist()}")
        
        # Apply field mapping (rename columns)
        field_mapping = session['field_mapping']
        print(f"Field mapping: {field_mapping}")
        
        rename_dict = {old_name: new_name for new_name, old_name in field_mapping.items() 
                      if old_name and old_name != 'none'}
        print(f"Rename dictionary: {rename_dict}")
        
        df.rename(columns=rename_dict, inplace=True)
        print(f"Columns after renaming: {df.columns.tolist()}")
        
        # Handle rowID if not mapped
        if 'rowID' not in df.columns:
            df['rowID'] = range(1, len(df) + 1)
            session['field_types']['rowID'] = 'integer'
            print("Added rowID column")
        
        # Handle chronological information
        chrono_info = session['chronological_info']
        print(f"Chronological info: {chrono_info}")
        
        if chrono_info['has_chronological']:
            if chrono_info['single_field']:
                year_column = chrono_info['year_column']
                print(f"Looking for single year column: {year_column}")
                print(f"Available columns: {df.columns.tolist()}")
                
                # Check if the column exists in the dataframe
                if year_column in df.columns:
                    df.rename(columns={year_column: 'ref_START'}, inplace=True)
                    df['ref_END'] = df['ref_START']
                    print(f"Renamed {year_column} to ref_START and copied to ref_END")
                else:
                    print(f"ERROR: Column '{year_column}' not found in dataframe!")
                    print(f"This might be because the column was renamed in field mapping.")
                    
                    # Try to find the column in the reverse mapping
                    reverse_mapping = {new_name: old_name for new_name, old_name in field_mapping.items()}
                    print(f"Reverse mapping: {reverse_mapping}")
                    
                    # Check if the year_column was actually renamed
                    mapped_name = None
                    for new_name, old_name in field_mapping.items():
                        if old_name == year_column:
                            mapped_name = new_name
                            break
                    
                    if mapped_name and mapped_name in df.columns:
                        print(f"Found column was renamed to: {mapped_name}")
                        df.rename(columns={mapped_name: 'ref_START'}, inplace=True)
                        df['ref_END'] = df['ref_START']
                        print(f"Used renamed column {mapped_name} for chronological data")
                    else:
                        print(f"Could not find year column anywhere. Setting default values.")
                        df['ref_START'] = 1111
                        df['ref_END'] = 9999
            else:
                start_col = chrono_info['start_year_column']
                end_col = chrono_info['end_year_column']
                print(f"Looking for start column: {start_col}, end column: {end_col}")
                
                # Similar logic for start/end columns
                # Check if columns exist or were renamed
                for col_name, target_name in [(start_col, 'ref_START'), (end_col, 'ref_END')]:
                    if col_name in df.columns:
                        df.rename(columns={col_name: target_name}, inplace=True)
                        print(f"Renamed {col_name} to {target_name}")
                    else:
                        # Try to find in field mapping
                        mapped_name = None
                        for new_name, old_name in field_mapping.items():
                            if old_name == col_name:
                                mapped_name = new_name
                                break
                        
                        if mapped_name and mapped_name in df.columns:
                            df.rename(columns={mapped_name: target_name}, inplace=True)
                            print(f"Used renamed column {mapped_name} for {target_name}")
                        else:
                            print(f"Could not find {col_name}. Setting default for {target_name}")
                            df[target_name] = 1111 if target_name == 'ref_START' else 9999
        else:
            df['ref_START'] = 1111
            df['ref_END'] = 9999
            print("No chronological info, set default values")
        
        print(f"Columns after chronological processing: {df.columns.tolist()}")
        
        # Clean and coerce data
        field_types = session['field_types']
        print(f"Field types: {field_types}")
        
        cleaned_df, legacy_columns = clean_and_coerce_data(df, field_types)
        print(f"Final cleaned dataframe shape: {cleaned_df.shape}")
        print(f"Final columns: {cleaned_df.columns.tolist()}")
        
        # Save cleaned CSV. Use the SAVED (possibly de-duplicated) upload name as
        # the base so the cleaned file aligns with the _formatted file and the
        # downstream matching prefix (e.g. padroncharcas_1 -> ..._cleaned.csv).
        original_filename = session['original_filename']
        base_saved = os.path.splitext(session.get('uploaded_file', original_filename))[0]
        session['prefix'] = base_saved
        output_filename = f"{base_saved}_cleaned.csv"
        output_path = os.path.join(current_app.config['UPLOAD_FOLDER'], output_filename)
        cleaned_df.to_csv(output_path, sep=";", index=False, encoding="utf-8")
        print(f"Saved cleaned CSV to: {output_path}")
        
        # Store results in session
        session['output_filename'] = output_filename
        session['legacy_columns'] = legacy_columns
        session['final_rows'] = len(cleaned_df)
        session['final_columns'] = len(cleaned_df.columns)
        
        return render_template('process_complete.html',
                             original_filename=original_filename,
                             output_filename=output_filename,
                             rows=len(cleaned_df),
                             columns=len(cleaned_df.columns),
                             legacy_columns=legacy_columns,
                             # metadata & licensing is required before matching
                             metadata_done=_metadata_exists(base_saved),
                             session=session)  # Pass session for debugging
        
    except Exception as e:
        print(f"Exception in process_final: {str(e)}")
        import traceback
        traceback.print_exc()
        
        # Store error info in session for debugging
        session['error_info'] = {
            'error': str(e),
            'traceback': traceback.format_exc(),
            'field_mapping': session.get('field_mapping', {}),
            'chronological_info': session.get('chronological_info', {}),
            'columns': session.get('columns', []),
            'field_types': session.get('field_types', {})
        }
        
        return render_template('error.html', 
                             error=str(e),
                             session=session)  # Pass session for debugging


CC_BY_SA = "http://creativecommons.org/licenses/by-sa/4.0/"
EMBARGO_YEARS = {"immediate": 0, "1year": 1, "2years": 2, "3years": 3}


def _plus_years(d, years):
    try:
        return d.replace(year=d.year + years)
    except ValueError:  # Feb 29
        return d.replace(month=2, day=28, year=d.year + years)


ARCA_PUBLISHER = "ARCA"


def _clean_list(values):
    """Trimmed, non-empty values from a repeatable form field."""
    return [v.strip() for v in (values or []) if v and v.strip()]


def _build_citation(creators, title, publishers, date_submitted):
    """The citation is always generated here, never supplied by the user:

        Creator, "Title": An ARCA [- Publisher2 - Publisher3] Dataset. YYYY-MM-DD.
    """
    who = ", ".join(creators)
    extra = [p for p in publishers if p.strip().lower() != ARCA_PUBLISHER.lower()]
    pubs = " - ".join([ARCA_PUBLISHER] + extra)
    head = f'{who}, "{title}"' if who else f'"{title}"'
    return f'{head}: An {pubs} Dataset. {date_submitted}.'


def _dataset_columns(base):
    """Column names of the cleaned table, for the field-definition helper."""
    path = os.path.join(current_app.config['UPLOAD_FOLDER'], f"{base}_cleaned.csv")
    if os.path.exists(path):
        try:
            return list(pd.read_csv(path, sep=';', nrows=0, encoding='utf-8').columns)
        except Exception:
            pass
    return list(session.get('columns') or [])


def _build_dublin_core(f, accept, embargo_key, base, extras=None):
    """Build the Dublin Core metadata text (one tag per line)."""
    from datetime import date
    esc = lambda s: (str(s or "").strip()
                     .replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
    extras = extras or {}
    today = date.today()
    date_submitted = today.isoformat()          # YYYY-MM-DD

    creators = f.get("creators") or []
    publishers = f.get("publishers") or []
    sources = f.get("sources") or []

    lines = []
    # "Spatial data: " is prepended to the title automatically.
    lines.append(f"<dc:title>Spatial data: {esc(f.get('title'))}</dc:title>")
    for c in creators:
        lines.append(f"<dc:creator>{esc(c)}</dc:creator>")
    for s in (f.get("subjects") or "").split(","):
        if s.strip():
            lines.append(f"<dc:subject>{esc(s)}</dc:subject>")
    if f.get("description"):
        lines.append(f"<dc:description>{esc(f.get('description'))}</dc:description>")

    # ARCA is always a publisher; user-supplied ones follow.
    for p in [ARCA_PUBLISHER] + [x for x in publishers
                                 if x.strip().lower() != ARCA_PUBLISHER.lower()]:
        lines.append(f"<dc:publisher>{esc(p)}</dc:publisher>")
    if f.get("contributor"):
        lines.append(f"<dc:contributor>{esc(f.get('contributor'))}</dc:contributor>")

    lines.append(f"<dcterms:dateSubmitted>{date_submitted}</dcterms:dateSubmitted>")
    lines.append(f"<dcterms:issued>{date_submitted}</dcterms:issued>")
    years = EMBARGO_YEARS.get(embargo_key, 0)
    if accept and years > 0:
        lines.append(f"<dcterms:available>{_plus_years(today, years).isoformat()}</dcterms:available>")

    lines.append(f"<dc:type>{esc(f.get('type') or 'Dataset')}</dc:type>")
    citation = _build_citation(creators, f.get("title"), publishers, date_submitted)
    lines.append(f"<dcterms:bibliographicCitation>{esc(citation)}</dcterms:bibliographicCitation>")

    for s in sources:
        lines.append(f"<dc:source>{esc(s)}</dc:source>")
    if extras.get("bibliography"):
        lines.append(f"<dc:source>Bibliography file: {esc(base)}_bibliography.txt</dc:source>")
    if extras.get("field_definitions"):
        lines.append(f"<dcterms:hasPart>{esc(base)}_field_definitions.csv</dcterms:hasPart>")

    if f.get("spatial"):
        lines.append(f"<dcterms:spatial>{esc(f.get('spatial'))}</dcterms:spatial>")
    if f.get("coverage"):
        lines.append(f"<dc:coverage>{esc(f.get('coverage'))}</dc:coverage>")

    if accept:
        lines.append(f"<dc:rights>{CC_BY_SA}</dc:rights>")
    else:
        lines.append("<dc:rights>All rights reserved. Contact the site owner "
                     "(Arca de las Indias) for an individual licensing solution.</dc:rights>")
    return "\n".join(lines) + "\n"


@bp.route('/metadata', methods=['GET', 'POST'])
def metadata_form():
    # Metadata now happens AFTER matching and is reachable by prefix alone (e.g.
    # resuming a finished run in a fresh browser), so accept ?prefix / form prefix
    # and fall back to the session only when none is given.
    base = (request.values.get('prefix', '').strip()
            or session.get('prefix')
            or (os.path.splitext(session['uploaded_file'])[0]
                if session.get('uploaded_file') else ''))
    if not base:
        return redirect(url_for('.upload_csv'))
    folder = current_app.config['UPLOAD_FOLDER']
    columns = _dataset_columns(base)

    if request.method == 'POST':
        fields = {
            "title": request.form.get("title", "").strip(),
            "creators": _clean_list(request.form.getlist("creator")),
            "publishers": _clean_list(request.form.getlist("publisher")),
            "sources": _clean_list(request.form.getlist("source")),
            "subjects": request.form.get("subjects", ""),
            "description": request.form.get("description", "").strip(),
            "contributor": request.form.get("contributor", ""),
            "type": request.form.get("type", ""),
            "spatial": request.form.get("spatial", ""),
            "coverage": request.form.get("coverage", ""),
        }
        accept = request.form.get("license") == "accept"
        embargo = request.form.get("embargo", "immediate")

        # --- bibliography upload (optional alternative to listing sources) ---
        biblio = request.files.get("bibliography")
        biblio_saved = False
        if biblio and biblio.filename:
            if not biblio.filename.lower().endswith(".txt"):
                return render_template('metadata_form.html', base=base, saved=False,
                                       columns=columns, form=fields,
                                       errors=["The bibliography must be a .txt file."])
            biblio.save(os.path.join(folder, f"{base}_bibliography.txt"))
            biblio_saved = True
        elif os.path.exists(os.path.join(folder, f"{base}_bibliography.txt")):
            biblio_saved = True

        # --- required fields ---
        errors = []
        if not fields["title"]:
            errors.append("A title is required.")
        if not fields["creators"]:
            errors.append("At least one creator is required.")
        if not fields["description"]:
            errors.append("A description is required.")
        if not fields["sources"] and not biblio_saved:
            errors.append("At least one source is required — list it, or upload a "
                          "bibliography .txt file.")
        if errors:
            return render_template('metadata_form.html', base=base, saved=False,
                                   columns=columns, form=fields, errors=errors)

        # --- optional per-column definitions ---
        defs = [(c, request.form.get(f"def__{c}", "").strip()) for c in columns]
        defs = [(c, d) for c, d in defs if d]
        if defs:
            with open(os.path.join(folder, f"{base}_field_definitions.csv"), "w",
                      encoding="utf-8", newline="") as fh:
                w = csv.writer(fh, delimiter=";")
                w.writerow(["column", "definition"])
                w.writerows(defs)

        text = _build_dublin_core(fields, accept, embargo, base,
                                  extras={"bibliography": biblio_saved,
                                          "field_definitions": bool(defs)})
        with open(os.path.join(folder, f"{base}_metadata.txt"), "w", encoding="utf-8") as fh:
            fh.write(text)
        return render_template('metadata_form.html', base=base, saved=True,
                               preview=text, accepted=accept,
                               biblio_saved=biblio_saved, definitions=len(defs))

    return render_template('metadata_form.html', base=base, saved=False,
                           columns=columns, form=None, errors=None)


@bp.route('/download/<matched_filename>')
def download_file(matched_filename):
    try:
        filepath = os.path.join(current_app.config['UPLOAD_FOLDER'], matched_filename)
        return send_file(filepath, as_attachment=True)
    except Exception as e:
        return render_template('error.html', error=f'File not found: {str(e)}')


