from flask import render_template, redirect, url_for, session, send_file
import os
from . import bp

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
GUIDEBOOK_DIR = os.path.join(BASE_DIR, "data", "guidebooks")

# Decision tree structure (same as before)
DECISION_TREE = {
    'start': {
        'question': 'What is your goal?',
        'image': '/static/images/symbols/goal_selection.png',
        'options': [
            {'id': '1.1', 'text': 'I have data for integration', 'next': '1.1'},
            {'id': '1.2', 'text': 'I want to create data connecting to HGIS-Indias/ARCA', 'next': '1.2'},
            {'id': '1.3', 'text': 'I want to contribute to existing ARCA or HGIS-Indias datasets', 'next': '1.3'}
        ]
    },
    '1.1': {
        'question': 'What is the domain of your data?',
        'image': '/static/images/symbols/data_type.png',
        'options': [
            {'id': '1.1.1', 'text': 'Data on colonial Spanish America', 'next': '1.1.1'},
            {'id': '1.1.2', 'text': 'Data not on colonial Spanish America', 'next': '1.1.2'}
        ]
    },
    '1.1.1': {
        'question': 'Does your data have a geographic dimension?',
        'image': '/static/images/symbols/geo_dimension.png',
        'options': [
            {'id': '1.1.1.a', 'text': 'Explicit geodata (shapefiles, GeoJSON, …)', 'next': '1.1.1.a'},
            {'id': '1.1.1.b', 'text': 'Data with a geographic component (place names, coordinates, …)', 'next': '1.1.1.b'},
            {'id': '1.1.1.c', 'text': 'No geographic component', 'next': '1.1.1.c'}
        ]
    },
    # (c) no geographic component -> out of scope
    '1.1.1.c': {
        'question': 'Data without a geographic dimension',
        'image': '/static/images/symbols/no_geo.png',
        'is_endpoint': True,
        'message': 'ARCA connects data through a shared geographic frame, so integration needs a spatial dimension. But it is not outside of our scope of interest. This might be the start of something new: <strong>If you have funding or seek funding or cooperation, please <a href="/team">contact us</a>.</strong>'
    },
    # (a) explicit geodata -> settlements/territories?
    '1.1.1.a': {
        'question': 'Does your geodata refer to settlements and/or administrative territories?',
        'image': '/static/images/symbols/vector_content.png',
        'options': [
            {'id': '1.1.1.a.1', 'text': 'Yes — settlements and/or administrative territories', 'next': '1.1.1.a.1'},
            {'id': '1.1.1.a.2', 'text': 'No — other geodata (locations, routes, other areas)', 'next': '1.1.1.a.2'}
        ]
    },
    '1.1.1.a.1': {
        'question': 'Match your geodata to our database',
        'image': '/static/images/symbols/vector_settlements.png',
        'is_endpoint': True,
        'message': 'Great — you can match your attribute table to our database, giving your features stable IDs and increasing interoperability with other data. Read the guidebook and proceed to the upload & matching workflow.',
        'actions': [
            {'text': 'Download Guidebook: "Upload and process file"', 'link': '/orientation/guidebook/1'},
            {'text': 'Access Upload File Processor', 'link': '/transform/'}
        ]
    },
    '1.1.1.a.2': {
        'question': 'Standalone Geodata',
        'image': '/static/images/symbols/other_vector.png',
        'is_endpoint': True,
        'message': 'Your geodata is not primarily about settlements or territories, but it could still be highly relevant.<br><strong>We may still be interested in repositing or linking up your research data.</strong><br><strong>Please <a href="/team">contact us</a>.</strong>'
    },
    # (b) data with a geographic component -> nature of the data
    '1.1.1.b': {
        'question': 'What is the nature of your data?',
        'image': '/static/images/symbols/data_nature.png',
        'options': [
            {'id': '1.1.1.b.1', 'text': 'Data related to settlements and/or administrative territories', 'next': '1.1.1.b.1'},
            {'id': '1.1.1.b.2', 'text': 'Data not related to settlements/territories', 'next': '1.1.1.b.2'}
        ]
    },
    # (b)(x) related to settlements -> time period
    '1.1.1.b.1': {
        'question': 'What time period does your data cover?',
        'image': '/static/images/symbols/time_period.png',
        'options': [
            {'id': '1.1.1.b.1.fit1', 'text': '1701-1808', 'next': '1.1.1.b.fit'},
            {'id': '1.1.1.b.1.fit2', 'text': 'Somewhat earlier or later (~1680-1825)', 'next': '1.1.1.b.fit'},
            {'id': '1.1.1.b.1.out', 'text': 'Otherwise (earlier or later)', 'next': '1.1.1.b.out'}
        ]
    },
    '1.1.1.b.fit': {
        'question': 'Your data fits our scope',
        'image': '/static/images/symbols/upload_processor.png',
        'is_endpoint': True,
        'message': 'Your data fits our scope. Read the manuals and proceed to the upload & matching workflow to reconcile your place and territory names with our database.',
        'actions': [
            {'text': 'Download Guidebook: "Upload and process file"', 'link': '/orientation/guidebook/1'},
            {'text': 'Access Upload File Processor', 'link': '/transform/'}
        ]
    },
    '1.1.1.b.out': {
        'question': 'Your data is outside our current scope',
        'image': '/static/images/symbols/out_of_scope.png',
        'is_endpoint': True,
        'message': 'We are working on expanding our system, but currently data from periods earlier than ~1680 or later than ~1825 likely won\'t fit our database well. But it is not outside of our scope of interest. This might be the start of something new: <strong>If you have funding or seek funding or cooperation, please <a href="/team">contact us</a>.</strong>'
    },
    # (b)(y) not related to settlements -> out of scope
    '1.1.1.b.2': {
        'question': 'Your data is outside our scope',
        'image': '/static/images/symbols/out_of_scope.png',
        'is_endpoint': True,
        'message': 'Data not related to settlements or territories is currently outside our scope. But it is not outside of our scope of interest. This might be the start of something new: <strong>If you have funding or seek funding or cooperation, please <a href="/team">contact us</a>.</strong>'
    },
    '1.1.2': {
        'question': 'Your data is outside our scope.',
        'image': '/static/images/symbols/out_of_scope.png',
        'is_endpoint': True,
        'message': 'Data not related to colonial Spanish America is outside our current scope. But it is not outside of our scope of interest. This might be the start of something new: <strong>If you have funding or seek funding or cooperation, please <a href="/team">contact us</a>.</strong><br><strong>If you have explicit geodata we may be interested in repositing/linking up your research data. Please <a href="/team">contact us</a>.</strong>'
    },
    '1.2': {
        'question': 'How would you like to create data?',
        'image': '/static/images/symbols/create_data.png',
        'options': [
            {'id': '1.2.1', 'text': 'I want to start with a totally empty template', 'next': '1.2.1'},
            {'id': '1.2.2', 'text': 'I want to pull a list of places or territories from the database', 'next': '1.2.2'},
            {'id': '1.2.3', 'text': 'I am not sure', 'next': '1.2.3'}
        ]
    },
    '1.2.1': {
        'question': 'Choose your template type:',
        'image': '/static/images/symbols/template_choice.png',
        'options': [
            {'id': '1.2.1.1', 'text': 'Places template', 'next': '1.2.1.1'},
            {'id': '1.2.1.2', 'text': 'Territories template', 'next': '1.2.1.2'}
        ]
    },
    '1.2.1.1': {
        'question': 'Get started with places data',
        'image': '/static/images/symbols/places_template.png',
        'is_endpoint': True,
        'message': 'Read our guidebook and get a template for place-related data.',
        'actions': [
            {'text': 'Download Guidebook: "Prepare data from scratch"', 'link': '/orientation/guidebook/3'},
            {'text': 'Download Empty Places Template', 'link': '/orientation/template/places'}
        ]
    },
    '1.2.1.2': {
        'question': 'Get started with territories data',
        'image': '/static/images/symbols/territories_template.png',
        'is_endpoint': True,
        'message': 'Read our guidebook and get a template for territory-related data.',
        'actions': [
            {'text': 'Download Guidebook: "Prepare data from scratch"', 'link': '/orientation/guidebook/3'},
            {'text': 'Download Empty Territories Template', 'link': '/orientation/template/territories'}
        ]
    },
    '1.2.2': {
        'question': 'Access pre-populated lists',
        'image': '/static/images/symbols/search_engine.png',
        'is_endpoint': True,
        'message': 'Read our guidebook on how to best search/assemble your prepopulated lists and proceed to the search engine.',
        'actions': [
            {'text': 'Download Guidebook: "Compile lists using search"', 'link': '/orientation/guidebook/4'},
            {'text': 'Search Engine Places', 'link': '/apps/enciclopedia/place/search'},
            {'text': 'Search Engine Territories', 'link': '/apps/enciclopedia/territory/search'}
        ]
    },
    '1.2.3': {
        'question': 'Help deciding between templates and pre-populated lists',
        'image': '/static/images/symbols/decision_help.png',
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
        'image': '/static/images/symbols/contribute.png',
        'is_endpoint': True,
        'message': 'Read our guidebook and proceed to our interface for contributions.',
        'actions': [
        {'text': 'Download Guidebook: "Improve HGIS-Indias"', 'link': '/orientation/guidebook/2'},
            {'text': 'Access Contribution Interface', 'link': '/contribute'}
        ]
    },
    '2': {
        'question': 'What is the nature of your data?',
        'image': '/static/images/symbols/data_nature_detail.png',
        'options': [
            {'id': '2.2', 'text': 'Without geographic component', 'next': '2.3'},
            {'id': '2.1', 'text': 'With geographic component', 'next': '2.1'},
            {'id': '2.3', 'text': 'Explicit geodata', 'next': '2.2'}
        ]
    },
    '2.3': {
        'question': 'Data without geographic component',
        'image': '/static/images/symbols/no_geo.png',
        'is_endpoint': True,
        'message': 'The data integration operates mostly on a spatial component. Contact us if you want a repository for your data anyway, or if you have an idea about how to integrate it.',
        'actions': [
            {'text': 'Contact Us', 'link': '/team'}
        ]
    },
    '2.1': {
        'question': 'What format is your geographic data in?',
        'image': '/static/images/symbols/geo_format.png',
        'options': [
            {'id': '2.2.1', 'text': 'Tabular (Excel, CSV.)', 'next': '2.2.1'},
            {'id': '2.2.2', 'text': 'Non-tabular: Semi or unstructured (text), document or image collection', 'next': '2.2.2'}
        ]
    },
    '2.2.1': {
        'question': 'Process your tabular data',
        'image': '/static/images/symbols/upload_processor.png',
        'is_endpoint': True,
        'message': 'Awesome, that is what we are looking for! Get our guidebook and move to upload file processor.',
        'actions': [
            {'text': 'Download Guidebook: "Upload and process file"', 'link': '/orientation/guidebook/1'},
            {'text': 'Access Upload File Processor', 'link': '/transform/'}
        ]
    },
    '2.2.2': {
        'question': 'Unstructured data processing',
        'image': '/static/images/symbols/unstructured.png',
        'is_endpoint': True,
        'message': 'Consider if your data is (also) convertible into a table or can be made accessible via a table (index; e.g. links to images). For annotated texts, individual solutions may be discussed, but there is no general workflow for integration (yet).',
        'actions': [
            {'text': 'Contact Us for Custom Solutions', 'link': '/team'}
        ]
    },
    '2.2': {
        'question': 'What type of geodata do you have?',
        'image': '/static/images/symbols/geodata_type.png',
        'options': [
            {'id': '2.3.1', 'text': 'Vector geodata', 'next': '2.3.1'},
            {'id': '2.3.4', 'text': 'Raster geodata', 'next': '2.3.4'}
        ]
    },
    '2.3.1': {
        'question': 'What does your vector geodata represent?',
        'image': '/static/images/symbols/vector_content.png',
        'options': [
            {'id': '2.3.2', 'text': 'Colonial settlements and/or administrative territories', 'next': '2.3.2'},
            {'id': '2.3.3', 'text': 'Other (pure locations, routes, different types of areas)', 'next': '2.3.3'}
        ]
    },
    '2.3.2': {
        'question': 'Vector geodata for settlements/territories',
        'image': '/static/images/symbols/vector_settlements.png',
        'is_endpoint': True,
        'message': 'Consider matching your attribute table(s) to our system/IDs, increasing interoperability with other data.',
        'actions': [
            {'text': 'Process as tabular data - Go to Upload Processor', 'link': '2.2.1'}
        ]
    },
    '2.3.3': {
        'question': 'Other vector geodata types',
        'image': '/static/images/symbols/other_vector.png',
        'is_endpoint': True,
        'message': 'We\'re intrigued to learn about your project! Please contact us.',
        'actions': [
            {'text': 'Contact Us', 'link': '/team'}
        ]
    },
    '2.3.4': {
        'question': 'Raster geodata processing',
        'image': '/static/images/symbols/raster_data.png',
        'is_endpoint': True,
        'message': 'Consider creating an index (with spatial component) of your collection and integrating that index with us.',
        'actions': [
            {'text': 'Process index as tabular data', 'link': '2.2.1'}
        ]
    }
}

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

@bp.route('/guidebook/<int:number>')
def guidebook(number):
    # Map guidebook numbers to filenames
    guidebook_files = {
        1: "Guidelines - Prepare and upload.docx",   # upload & process a file
        2: "Guidelines-Contribute.docx",             # improve HGIS-Indias
        3: "Guidelines-Create Data.docx",            # prepare data from scratch
        4: "Guidebook Search.docx",                  # compile lists using search
    }

    filename = guidebook_files.get(number)
    if not filename:
        return f"<h1>Guidebook {number}</h1><p>No file configured yet.</p><a href='{url_for('.index')}'>Back to start</a>"

    path = os.path.join(GUIDEBOOK_DIR, filename)
    if not os.path.exists(path):
        return f"<h1>Guidebook {number}</h1><p>File not found on server.</p><a href='{url_for('.index')}'>Back to start</a>"

    return send_file(path, as_attachment=True)


@bp.route('/template/<kind>')
def template(kind):
    """Serve the empty data-entry workbook.

    Templates.xlsx holds a 'Places', a 'Territories' and a 'combined' sheet, so
    the same workbook is served for either entry point.
    """
    if kind not in ('places', 'territories', 'combined'):
        return redirect(url_for('.index'))

    path = os.path.join(GUIDEBOOK_DIR, "Templates.xlsx")
    if not os.path.exists(path):
        return (f"<h1>Template</h1><p>File not found on server.</p>"
                f"<a href='{url_for('.index')}'>Back to start</a>")

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




