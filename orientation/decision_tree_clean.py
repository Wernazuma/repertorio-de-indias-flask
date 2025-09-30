from flask import Flask, render_template, request, redirect, url_for, session
import json

app = Flask(__name__)
app.secret_key = 'your-secret-key-here'

# Decision tree structure
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
        'message': '...but chances are that it fits. You may want to consider matching the data anyway and review the resulting table so that the date fields fit your purposes. If so, go back a step and chose the first option.'
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
        'message': 'Read our guidebook (3) and get a template for place-related data.',
        'actions': [
            {'text': 'Download Guidebook 3: "Prepare data from scratch"', 'link': '/guidebook/3'},
            {'text': 'Download Empty Places Template', 'link': '/template/places'}
        ]
    },
    '1.2.1.2': {
        'question': 'Get started with territories data',
        'image': '/static/images/territories_template.png',
        'is_endpoint': True,
        'message': 'Read our guidebook (3) and get a template for territory-related data.',
        'actions': [
            {'text': 'Download Guidebook 3: "Prepare data from scratch"', 'link': '/guidebook/3'},
            {'text': 'Download Empty Territories Template', 'link': '/template/territories'}
        ]
    },
    '1.2.2': {
        'question': 'Access pre-populated lists',
        'image': '/static/images/search_engine.png',
        'is_endpoint': True,
        'message': 'Read our guidebook (4) on how to best search/assemble your prepopulated lists and proceed to the search engine.',
        'actions': [
            {'text': 'Download Guidebook 4: "Extract places and territories using the search"', 'link': '/guidebook/4'},
            {'text': 'Access Search Engine', 'link': '/search-engine'}
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
        'message': 'Read our guidebook (2) and proceed to our interface for contributions.',
        'actions': [
            {'text': 'Download Guidebook 2: "Improve HGIS-Indias"', 'link': '/guidebook/2'},
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
            {'id': '2.2.1', 'text': 'Tabular (Excel, CSV...)', 'next': '2.2.1'},
            {'id': '2.2.2', 'text': 'Non-tabular: Semi or unstructured (text), document or image collection', 'next': '2.2.2'}
        ]
    },
    '2.2.1': {
        'question': 'Process your tabular data',
        'image': '/static/images/upload_processor.png',
        'is_endpoint': True,
        'message': 'Get our guidebook (1) and move to upload file processor.',
        'actions': [
            {'text': 'Download Guidebook 1: "Upload and process file"', 'link': '/guidebook/1'},
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

@app.route('/')
def index():
    session.clear()
    return redirect(url_for('step', step_id='start'))

@app.route('/step/<step_id>')
def step(step_id):
    if step_id not in DECISION_TREE:
        return redirect(url_for('index'))
    
    # Initialize or update breadcrumbs
    if 'breadcrumbs' not in session:
        session['breadcrumbs'] = []
    
    # Add current step to breadcrumbs if not already there
    current_step = DECISION_TREE[step_id]
    breadcrumb = {'id': step_id, 'text': current_step['question']}
    
    # Remove this step and any steps after it from breadcrumbs (for back navigation)
    session['breadcrumbs'] = [b for b in session['breadcrumbs'] if b['id'] != step_id]
    session['breadcrumbs'].append(breadcrumb)
    
    return render_template('workflow.html', 
                         step=current_step, 
                         step_id=step_id,
                         breadcrumbs=session['breadcrumbs'])

@app.route('/back/<step_id>')
def back_to_step(step_id):
    if step_id not in DECISION_TREE:
        return redirect(url_for('index'))
    
    # Update breadcrumbs to remove steps after the target step
    if 'breadcrumbs' in session:
        target_index = -1
        for i, breadcrumb in enumerate(session['breadcrumbs']):
            if breadcrumb['id'] == step_id:
                target_index = i
                break
        
        if target_index >= 0:
            session['breadcrumbs'] = session['breadcrumbs'][:target_index + 1]
    
    return redirect(url_for('step', step_id=step_id))

# Placeholder routes for external links
@app.route('/guidebook/<int:number>')
def guidebook(number):
    return f"<h1>Guidebook {number}</h1><p>This would download or display guidebook {number}.</p><a href='/'>Back to start</a>"

@app.route('/template/<template_type>')
def template(template_type):
    return f"<h1>{template_type.title()} Template</h1><p>This would download the {template_type} template.</p><a href='/'>Back to start</a>"

@app.route('/search-engine')
def search_engine():
    return "<h1>Search Engine</h1><p>This would be the search interface.</p><a href='/'>Back to start</a>"

@app.route('/contribute')
def contribute():
    return "<h1>Contribution Interface</h1><p>This would be the contribution interface.</p><a href='/'>Back to start</a>"

@app.route('/upload-processor')
def upload_processor():
    return "<h1>Upload File Processor</h1><p>This would be the file upload and processing interface.</p><a href='/'>Back to start</a>"

@app.route('/contact')
def contact():
    return "<h1>Contact Us</h1><p>This would be the contact form or information.</p><a href='/'>Back to start</a>"

if __name__ == '__main__':
    app.run(debug=True)
