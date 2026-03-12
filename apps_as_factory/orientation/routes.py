from flask import Flask, render_template, request, redirect, url_for, session, jsonify, send_file, current_app
import json
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

#app = Flask(__name__)
#app.secret_key = 'your-secret-key-here'
BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
GUIDEBOOK_DIR = os.path.join(BASE_DIR, "data", "guidebooks")
# Configuration for file uploads
UPLOAD_FOLDER = 'data/uploads'
ALLOWED_EXTENSIONS = {'csv'}
MAX_CONTENT_LENGTH = 16 * 1024 * 1024  # 16MB max file size



# Ensure upload directory exists
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# Decision tree structure (same as before)
DECISION_TREE = {
    'start': {
        'question': 'What is your goal?',
        'image': '/static/images/goal_selection.png',
        'options': [
            {'id': '1.1', 'text': 'I have data for integration', 'next': '1.1'},
            {'id': '1.2', 'text': 'I want to create data connecting to HGIS-Indias/ARCA', 'next': '1.2'},
            {'id': '1.3', 'text': 'I want to contribute to existing ARCA or HGIS-Indias datasets', 'next': '1.3'}
        ]
    },
    '1.1': {
        'question': 'What type of data do you have for integration?',
        'image': '/static/images/data_type.png',
        'options': [
            {'id': '1.1.1', 'text': 'Data on colonial Spanish America', 'next': '1.1.1'},
            {'id': '1.1.2', 'text': 'Data not on colonial Spanish America', 'next': '1.1.2'}
        ]
    },
    '1.1.1': {
        'question': 'What time period does your colonial Spanish America data cover?',
        'image': '/static/images/time_period.png',
        'options': [
            {'id': '1.1.1.1', 'text': '1701-1808', 'next': '1.1.1.1'},
            {'id': '1.1.1.2', 'text': 'Somewhat earlier or later (~1680-1824)', 'next': '1.1.1.2'},
            {'id': '1.1.1.3', 'text': 'Later or earlier', 'next': '1.1.1.3'}
        ]
    },
    '1.1.1.1': {
        'question': 'What is the nature of your data?',
        'image': '/static/images/data_nature.png',
        'options': [
            {'id': '1.1.1.1.1', 'text': 'Data related to settlements and/or administrative territories', 'next': '2'},
            {'id': '1.1.1.1.2', 'text': 'Data not related to settlements/territories', 'next': '1.1.1.1.2'}
        ],
    },
    '1.1.1.1.2': {
        'question': 'Your data is outside our scope.',
        'image': '/static/images/out_of_scope.png',
        'is_endpoint': True,
        'message': 'Unfortunately, data not related to settlements or territories from the late colonial Spanish America period (1701-1808) is outside our current scope.'
    },
    '1.1.1.2': {
        'question': 'Your data is slightly outside our scope (1701-1808),',
        'image': '/static/images/out_of_scope.png',
        'is_endpoint': True,
        'message': '.but chances are that it fits. You may want to consider matching the data anyway and review the resulting table so that the date fields fit your purposes. If so, go back a step and chose the first option.'
    },
    '1.1.1.3': {
        'question': 'Your data is outside our scope.',
        'image': '/static/images/out_of_scope.png',
        'is_endpoint': True,
        'message': 'We are working on expanding our system in the future, but currently data from periods earlier than 1680 or later than 1824 likely won\'t fit our database well.'
    },
    '1.1.2': {
        'question': 'Your data is outside our scope.',
        'image': '/static/images/out_of_scope.png',
        'is_endpoint': True,
        'message': 'Data not related to colonial Spanish America is outside our current scope.'
    },
    '1.2': {
        'question': 'How would you like to create data?',
        'image': '/static/images/create_data.png',
        'options': [
            {'id': '1.2.1', 'text': 'I want to start with a totally empty template', 'next': '1.2.1'},
            {'id': '1.2.2', 'text': 'I want to pull a list of places or territories from the database', 'next': '1.2.2'},
            {'id': '1.2.3', 'text': 'I am not sure', 'next': '1.2.3'}
        ]
    },
    '1.2.1': {
        'question': 'Choose your template type:',
        'image': '/static/images/template_choice.png',
        'options': [
            {'id': '1.2.1.1', 'text': 'Places template', 'next': '1.2.1.1'},
            {'id': '1.2.1.2', 'text': 'Territories template', 'next': '1.2.1.2'}
        ]
    },
    '1.2.1.1': {
        'question': 'Get started with places data',
        'image': '/static/images/places_template.png',
        'is_endpoint': True,
        'message': 'Read our guidebook and get a template for place-related data.',
        'actions': [
            {'text': 'Download Guidebook: "Prepare data from scratch"', 'link': '/orientation/guidebook/3'},
            {'text': 'Download Empty Places Template', 'link': '/template/places'}
        ]
    },
    '1.2.1.2': {
        'question': 'Get started with territories data',
        'image': '/static/images/territories_template.png',
        'is_endpoint': True,
        'message': 'Read our guidebook and get a template for territory-related data.',
        'actions': [
            {'text': 'Download Guidebook: "Prepare data from scratch"', 'link': '/orientation/guidebook/3'},
            {'text': 'Download Empty Territories Template', 'link': '/template/territories'}
        ]
    },
    '1.2.2': {
        'question': 'Access pre-populated lists',
        'image': '/static/images/search_engine.png',
        'is_endpoint': True,
        'message': 'Read our guidebook on how to best search/assemble your prepopulated lists and proceed to the search engine.',
        'actions': [
            {'text': 'Download Guidebook: "Compile lists using search"', 'link': '/orientation/guidebook/4'},
            {'text': 'Search Engine Places', 'link': '/places/search'},
            {'text': 'Search Engine Territories', 'link': '/territories/search'}
        ]
    },
    '1.2.3': {
        'question': 'Help deciding between templates and pre-populated lists',
        'image': '/static/images/decision_help.png',
        'is_endpoint': True,
        'message': '''Here are some arguments to help you decide:

You may want to start from scratch for these reasons:
• You already have data with assorted place names, want to convert it to a table and then match it.
• You have data entries that do not, or may not, correspond to places in the Indias database and thus do not want to work from a populated list.

You may want to work from a pre-populated list for these reasons:
• To avoid issues when matching (as each row already has an ID).
• To understand for which places you may need to compile data. E.g., you want to study tithes in a certain province. You pull a list for all parishes in that province in order to find for which places you should get data.''',
        'actions': [
            {'text': 'Go back to choose approach', 'link': '1.2'}
        ]
    },
    '1.3': {
        'question': 'Contribute to existing datasets',
        'image': '/static/images/contribute.png',
        'is_endpoint': True,
        'message': 'Read our guidebook and proceed to our interface for contributions.',
        'actions': [
        {'text': 'Download Guidebook: "Improve HGIS-Indias"', 'link': '/orientation/guidebook/2'},
            {'text': 'Access Contribution Interface', 'link': '/contribute'}
        ]
    },
    '2': {
        'question': 'What is the nature of your data?',
        'image': '/static/images/data_nature_detail.png',
        'options': [
            {'id': '2.2', 'text': 'Without geographic component', 'next': '2.3'},
            {'id': '2.1', 'text': 'With geographic component', 'next': '2.1'},
            {'id': '2.3', 'text': 'Explicit geodata', 'next': '2.2'}
        ]
    },
    '2.3': {
        'question': 'Data without geographic component',
        'image': '/static/images/no_geo.png',
        'is_endpoint': True,
        'message': 'The data integration operates mostly on a spatial component. Contact us if you want a repository for your data anyway, or if you have an idea about how to integrate it.',
        'actions': [
            {'text': 'Contact Us', 'link': '/contact'}
        ]
    },
    '2.1': {
        'question': 'What format is your geographic data in?',
        'image': '/static/images/geo_format.png',
        'options': [
            {'id': '2.2.1', 'text': 'Tabular (Excel, CSV.)', 'next': '2.2.1'},
            {'id': '2.2.2', 'text': 'Non-tabular: Semi or unstructured (text), document or image collection', 'next': '2.2.2'}
        ]
    },
    '2.2.1': {
        'question': 'Process your tabular data',
        'image': '/static/images/upload_processor.png',
        'is_endpoint': True,
        'message': 'Awesome, that is what we are looking for! Get our guidebook and move to upload file processor.',
        'actions': [
            {'text': 'Download Guidebook: "Upload and process file"', 'link': '/orientation/guidebook/1'},
            {'text': 'Access Upload File Processor', 'link': '/upload-processor'}
        ]
    },
    '2.2.2': {
        'question': 'Unstructured data processing',
        'image': '/static/images/unstructured.png',
        'is_endpoint': True,
        'message': 'Consider if your data is (also) convertible into a table or can be made accessible via a table (index; e.g. links to images). For annotated texts, individual solutions may be discussed, but there is no general workflow for integration (yet).',
        'actions': [
            {'text': 'Contact Us for Custom Solutions', 'link': '/contact'}
        ]
    },
    '2.2': {
        'question': 'What type of geodata do you have?',
        'image': '/static/images/geodata_type.png',
        'options': [
            {'id': '2.3.1', 'text': 'Vector geodata', 'next': '2.3.1'},
            {'id': '2.3.4', 'text': 'Raster geodata', 'next': '2.3.4'}
        ]
    },
    '2.3.1': {
        'question': 'What does your vector geodata represent?',
        'image': '/static/images/vector_content.png',
        'options': [
            {'id': '2.3.2', 'text': 'Colonial settlements and/or administrative territories', 'next': '2.3.2'},
            {'id': '2.3.3', 'text': 'Other (pure locations, routes, different types of areas)', 'next': '2.3.3'}
        ]
    },
    '2.3.2': {
        'question': 'Vector geodata for settlements/territories',
        'image': '/static/images/vector_settlements.png',
        'is_endpoint': True,
        'message': 'Consider matching your attribute table(s) to our system/IDs, increasing interoperability with other data.',
        'actions': [
            {'text': 'Process as tabular data - Go to Upload Processor', 'link': '2.2.1'}
        ]
    },
    '2.3.3': {
        'question': 'Other vector geodata types',
        'image': '/static/images/other_vector.png',
        'is_endpoint': True,
        'message': 'We\'re intrigued to learn about your project! Please contact us.',
        'actions': [
            {'text': 'Contact Us', 'link': '/contact'}
        ]
    },
    '2.3.4': {
        'question': 'Raster geodata processing',
        'image': '/static/images/raster_data.png',
        'is_endpoint': True,
        'message': 'Consider creating an index (with spatial component) of your collection and integrating that index with us.',
        'actions': [
            {'text': 'Process index as tabular data', 'link': '2.2.1'}
        ]
    }
}

# Utility functions from the original upload processor
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

def clean_and_coerce_data(df, field_types):
    """Clean and coerce data based on field types"""
    cleaned_df = df.copy()
    legacy_columns = {}

    available_columns = set(df.columns)
    
    for col, dtype in field_types.items():
        if col not in available_columns:
            continue
            
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
                continue
        except Exception as e:
            print(f"Error coercing field {col} to {dtype}: {e}")

        if dtype in ["integer", "decimal", "date"]:
            legacy_col = f"legacy_{col}"
            mask = cleaned_df[col].isna() & df[col].notna()
            legacy_data = df[col].where(mask)
            if legacy_data.notna().any():
                cleaned_df[legacy_col] = legacy_data
                legacy_columns[col] = legacy_col

    return cleaned_df, legacy_columns

# Original workflow routes
@bp.route('/')
def index():
    session.clear()
    return redirect(url_for('.step', step_id='start'))

@bp.route('/step/<step_id>')
def step(step_id):
    if step_id not in DECISION_TREE:
        return redirect(url_for('.index'))
    
    if 'breadcrumbs' not in session:
        session['breadcrumbs'] = []
    
    current_step = DECISION_TREE[step_id]
    breadcrumb = {'id': step_id, 'text': current_step['question']}
    
    session['breadcrumbs'] = [b for b in session['breadcrumbs'] if b['id'] != step_id]
    session['breadcrumbs'].append(breadcrumb)
    
    return render_template('workflow.html', 
                         step=current_step, 
                         step_id=step_id,
                         breadcrumbs=session['breadcrumbs'])

@bp.route('/back/<step_id>')
def back_to_step(step_id):
    if step_id not in DECISION_TREE:
        return redirect(url_for('.index'))
    
    if 'breadcrumbs' in session:
        target_index = -1
        for i, breadcrumb in enumerate(session['breadcrumbs']):
            if breadcrumb['id'] == step_id:
                target_index = i
                break
        
        if target_index >= 0:
            session['breadcrumbs'] = session['breadcrumbs'][:target_index + 1]
    
    return redirect(url_for('.step', step_id=step_id))

# File Upload Processor Route (Integrated Single Page)
@bp.route('/upload-processor')
def upload_processor():
    # Clear any existing processor session data
    processor_keys = ['uploaded_file', 'original_filename', 'domain', 'csv_rows', 
                     'csv_columns', 'formatted_path', 'field_types', 'field_samples', 
                     'columns', 'field_mapping', 'chronological_info', 'output_filename']
    for key in processor_keys:
        session.pop(key, None)
    
    return render_template('upload_processor.html')

# API endpoints for the upload processor
@bp.route('/api/upload', methods=['POST'])
def api_upload():
    if 'file' not in request.files:
        return jsonify({'error': 'No file selected'}), 400
    
    file = request.files['file']
    if file.filename == '':
        return jsonify({'error': 'No file selected'}), 400
    
    if file and allowed_file(file.filename):
        filename = secure_filename(file.filename)
        filepath = os.path.join(current_app.config['UPLOAD_FOLDER'], filename)
        
        counter = 1
        original_filename = filename
        while os.path.exists(filepath):
            name, ext = os.path.splitext(original_filename)
            filename = f"{name}_{counter}{ext}"
            filepath = os.path.join(current_app.config['UPLOAD_FOLDER'], filename)
            counter += 1
        
        file.save(filepath)
        
        session['uploaded_file'] = filename
        session['original_filename'] = file.filename
        
        return jsonify({'success': True, 'filename': filename})
    
    return jsonify({'error': 'Invalid file type. Please upload a CSV file.'}), 400

@bp.route('/api/process_domain', methods=['POST'])
def api_process_domain():
    if 'uploaded_file' not in session:
        return jsonify({'error': 'No file uploaded'}), 400
    
    domain = request.json.get('domain')
    if domain not in ['places', 'territories']:
        return jsonify({'error': 'Invalid domain selection'}), 400
    
    session['domain'] = domain
    
    filepath = os.path.join(current_app.config['UPLOAD_FOLDER'], session['uploaded_file'])
    
    try:
        detected_sep, text_qualifier, decimal_marker = detect_csv_format(filepath)
        formatted_path = convert_to_expected_format(filepath, detected_sep, text_qualifier, decimal_marker)
        
        df = pd.read_csv(formatted_path, sep=";", encoding='utf-8')
        
        session['csv_rows'] = len(df)
        session['csv_columns'] = len(df.columns)
        session['formatted_path'] = formatted_path
        
        field_types, field_samples = detect_and_classify_columns(df)
        
        session['field_types'] = field_types
        session['field_samples'] = field_samples
        session['columns'] = df.columns.tolist()
        
        # Check if any columns need classification
        needs_classification = {col: field_samples[col] 
                              for col, ftype in field_types.items() 
                              if ftype == 'needs_classification'}
        
        return jsonify({
            'success': True,
            'rows': len(df),
            'columns': len(df.columns),
            'needs_classification': needs_classification
        })
        
    except Exception as e:
        return jsonify({'error': f'Error processing CSV: {str(e)}'}), 500

@bp.route('/api/save_column_types', methods=['POST'])
def api_save_column_types():
    if 'field_types' not in session:
        return jsonify({'error': 'No field types in session'}), 400

    column_types = request.json.get('column_types', {})
    
    # Update session types
    for col, col_type in column_types.items():
        session['field_types'][col] = col_type

    # Determine integer columns
    integer_columns = session.get('integer_columns', [])
    session['integer_columns'] = integer_columns

    return jsonify({'success': True})


@bp.route('/api/get_field_mapping_info', methods=['GET'])
def api_get_field_mapping_info():
    if 'field_types' not in session or 'domain' not in session:
        return jsonify({'error': 'Missing session data'}), 400
    
    domain = session['domain']
    columns = session['columns']
    field_types = session['field_types']
    
    if domain == 'places':
        required_fields = [
            {'name': 'rowID', 'label': 'Unique ID of each row', 'types': ['integer', 'text', 'string'], 'optional': True},
            {'name': 'internalID', 'label': 'Internal ID for each entity', 'types': ['integer', 'text', 'string'], 'optional': True},
            {'name': 'ref_Label', 'label': 'Associated place\'s name', 'types': ['text', 'string'], 'optional': False},
            {'name': 'ref_Categoria', 'label': 'Associated place\'s generic settlement type', 'types': ['text', 'string'], 'optional': True},
            {'name': 'ref_Year', 'label': 'Reference year for the row', 'types': ['integer'], 'optional': True},
            {'name': 'ref_START', 'label': 'Reference start year for the row', 'types': ['integer'], 'optional': True},
            {'name': 'ref_END', 'label': 'Reference end year for the row', 'types': ['integer'], 'optional': True},
            {'name': 'ref_Variantes', 'label': 'Variant or variants of the name', 'types': ['text', 'string'], 'optional': True},
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
            {'name': 'rowID', 'label': 'Internal ID of each row', 'types': ['integer', 'text', 'string'], 'optional': True},
            {'name': 'internalID', 'label': 'Internal ID for each entity', 'types': ['integer', 'text', 'string'], 'optional': True},
            {'name': 'ref_Label', 'label': 'Associated place\'s name', 'types': ['text', 'string'], 'optional': False},
            {'name': 'ref_Year', 'label': 'Reference year for the row', 'types': ['text', 'string'], 'optional': True},
            {'name': 'ref_START', 'label': 'Reference start year for the row', 'types': ['integer'], 'optional': True},
            {'name': 'ref_Variantes', 'label': 'Variant or variants of the name', 'types': ['text', 'string'], 'optional': True},
            {'name': 'ref_Titulo', 'label': 'Territory title (alcaldía mayor, corregimiento, etc.)', 'types': ['text', 'string'], 'optional': True},
            {'name': 'ref_Nivel', 'label': 'Hierarchical level or general type', 'types': ['text', 'string'], 'optional': True},
            {'name': 'ref_Region', 'label': 'Generic region it is in', 'types': ['text', 'string'], 'optional': True},
        ]
    
    filtered_columns = {}
    for field in required_fields:
        allowed_cols = [col for col, ftype in field_types.items() 
                       if ftype in field['types']]
        filtered_columns[field['name']] = allowed_cols
    
    # Get integer columns for chronological info
    integer_columns = [col for col, ftype in field_types.items() if ftype == 'integer']
    
    return jsonify({
        'required_fields': required_fields,
        'filtered_columns': filtered_columns,
        'integer_columns': integer_columns,
        'domain': domain
    })

@bp.route('/api/process_final', methods=['POST'])
def api_process_final():
    try:
        data = request.json
        field_mapping = data.get('field_mapping', {})
        chronological_info = data.get('chronological_info', {})
        
        session['field_mapping'] = field_mapping
        session['chronological_info'] = chronological_info
        
        # Load the CSV
        filepath = session['formatted_path']
        df = pd.read_csv(filepath, sep=";", encoding='utf-8')
        
        # Apply field mapping (rename columns)
        rename_dict = {old_name: new_name for new_name, old_name in field_mapping.items() 
                      if old_name and old_name != 'none'}
        
        df.rename(columns=rename_dict, inplace=True)
        
        # Handle rowID if not mapped
        if 'rowID' not in df.columns:
            df['rowID'] = range(1, len(df) + 1)
            session['field_types']['rowID'] = 'integer'
        
        # Handle chronological information
        if chronological_info.get('has_chronological'):
            if chronological_info.get('single_field'):
                year_column = chronological_info.get('year_column')
                
                # Check if column exists or was renamed
                if year_column in df.columns:
                    df.rename(columns={year_column: 'ref_START'}, inplace=True)
                    df['ref_END'] = df['ref_START']
                else:
                    # Try to find in field mapping
                    mapped_name = None
                    for new_name, old_name in field_mapping.items():
                        if old_name == year_column:
                            mapped_name = new_name
                            break
                    
                    if mapped_name and mapped_name in df.columns:
                        df.rename(columns={mapped_name: 'ref_START'}, inplace=True)
                        df['ref_END'] = df['ref_START']
                    else:
                        df['ref_START'] = 1111
                        df['ref_END'] = 9999
            else:
                start_col = chronological_info.get('start_year_column')
                end_col = chronological_info.get('end_year_column')
                
                for col_name, target_name in [(start_col, 'ref_START'), (end_col, 'ref_END')]:
                    if col_name in df.columns:
                        df.rename(columns={col_name: target_name}, inplace=True)
                    else:
                        # Try to find in field mapping
                        mapped_name = None
                        for new_name, old_name in field_mapping.items():
                            if old_name == col_name:
                                mapped_name = new_name
                                break
                        
                        if mapped_name and mapped_name in df.columns:
                            df.rename(columns={mapped_name: target_name}, inplace=True)
                        else:
                            df[target_name] = 1111 if target_name == 'ref_START' else 9999
        else:
            df['ref_START'] = 1111
            df['ref_END'] = 9999
        
        # Clean and coerce data
        field_types = session['field_types']
        cleaned_df, legacy_columns = clean_and_coerce_data(df, field_types)
        
        # Save cleaned CSV
        original_filename = session['original_filename']
        output_filename = original_filename.replace('.csv', '_cleaned.csv')
        output_path = os.path.join(current_app.config['UPLOAD_FOLDER'], output_filename)
        cleaned_df.to_csv(output_path, sep=";", index=False, encoding="utf-8")
        
        # Store results in session
        session['output_filename'] = output_filename
        session['legacy_columns'] = legacy_columns
        session['final_rows'] = len(cleaned_df)
        session['final_columns'] = len(cleaned_df.columns)
        
        return jsonify({
            'success': True,
            'output_filename': output_filename,
            'rows': len(cleaned_df),
            'columns': len(cleaned_df.columns),
            'legacy_columns': legacy_columns
        })
        
    except Exception as e:
        return jsonify({'error': f'Error processing final data: {str(e)}'}), 500

@bp.route('/api/download/<filename>')
def api_download(filename):
    try:
        filepath = os.path.join(current_app.config['UPLOAD_FOLDER'], filename)
        return send_file(filepath, as_attachment=True)
    except Exception as e:
        return jsonify({'error': f'File not found: {str(e)}'}), 404

@bp.route('/guidebook/<int:number>')
def guidebook(number):
    # Map guidebook numbers to filenames
    guidebook_files = {
        2: "Guidelines-Contribute.docx",
        # add others here when you know their filenames, e.g.:
        # 1: "Guidelines-Upload-and-process-file.docx",
        # 3: "Guidelines-Prepare-data-from-scratch.docx",
        # 4: "Guidelines-Compile-lists-using-search.docx",
    }

    filename = guidebook_files.get(number)
    if not filename:
        return f"<h1>Guidebook {number}</h1><p>No file configured yet.</p><a href='{url_for('.index')}'>Back to start</a>"

    path = os.path.join(GUIDEBOOK_DIR, filename)
    if not os.path.exists(path):
        return f"<h1>Guidebook {number}</h1><p>File not found on server.</p><a href='{url_for('.index')}'>Back to start</a>"

    return send_file(path, as_attachment=True)


@bp.route('/contribute')
def contribute():
    external_url = "https://ehess.maps.arcgis.com/apps/webappviewer/index.html?id=3981981d9db8436a8a1015f83391ec0a"
    return f"""
    <h1>Contribution Interface</h1>
    <p>
      This will open the ARCA / HGIS-Indias contribution interface in a new tab:
    </p>
    <p>
      <a href="{external_url}" target="_blank" rel="noopener noreferrer">
        Open contribution interface
      </a>
    </p>
    <p><a href="{url_for('.index')}">Back to start</a></p>
    """

@bp.route('/contact')
def contact():
    return f"""
    <h1>Contact Us</h1>
    <p>
      You can reach us by email. Click the link below to reveal the address:
    </p>
    <p>
      <a id="email-link" href="#" onclick="revealEmail(); return false;">
        Show email address
      </a>
    </p>
    <script>
      function revealEmail() {{
        var user = "werner.stangl";
        var domain = "gmail.com";
        var addr = user + "@" + domain;
        var link = document.getElementById("email-link");
        link.href = "mailto:" + addr;
        link.textContent = addr;
      }}
    </script>
    <p><a href="{url_for('.index')}">Back to start</a></p>
    """

# Placeholder routes for external links (same as before)

@bp.route('/template/<template_type>')
def template(template_type):
    return f"<h1>{template_type.title()} Template</h1><p>This would download the {template_type} template.</p><a href='/'>Back to start</a>"

@bp.route('/search-engine')
def search_engine():
    return "<h1>Search Engine</h1><p>This would be the search interface.</p><a href='/'>Back to start</a>"



