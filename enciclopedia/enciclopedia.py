import csv, os, unicodedata, re
from flask import Flask, render_template, request, jsonify, Response, send_from_directory, abort
from fuzzywuzzy import fuzz
from io import StringIO

# Configure Flask
app = Flask(__name__, static_url_path='/static')

# Function to read data from CSV files
def read_csv(filename):
    data = []
    with open(filename, 'r', encoding='utf-8') as file:
        reader = csv.DictReader(file, delimiter=';')
        for row in reader:
            data.append(row)
    return data

# Read the CSV files
gz_entidades_csv = read_csv('../data/gz_entidades.csv')
church_csv = read_csv('../data/gz_iglesia.csv')
name_csv = read_csv('../data/gz_nombres.csv')
category_csv = read_csv('../data/gz_categoria.csv')
geometry_csv = read_csv('../data/gz_geometry.csv')
cabildo_csv = read_csv('../data/gz_cabildo.csv')
foreignkeys_csv = read_csv('../data/gz_foreignkeys.csv')
espartede_csv = read_csv('../data/espartede.csv')
contains_csv = read_csv('../data/contains.csv')
gz_info_csv = read_csv('../data/gz_info_1.csv')
capital_csv = read_csv('../data/Cabeceras-Entidades.csv')
institution_csv = read_csv('../data/instituciones.csv')

oficiales_entity_csv = read_csv('../data/da_adm_Oficial_link.csv')
infotable_csv = read_csv('../data/infotable.csv')
fuentes_entidades_csv = read_csv('../data/entidades_fuentes.csv')
entidades_csv = read_csv('../data/entidades.csv')
jerarquia_csv = read_csv('../data/jerarquia.csv')

fuentes_csv = read_csv('../data/fuentes.csv')
oficiales_csv = read_csv('../data/da_adm_Oficiales.csv')
oficiales_foreignkeys_csv = read_csv('../data/da_adm_Oficiales_foreignkeys.csv')


########################
### Helper functions ###
########################

def stopwordremove(inputstring):
    # Remove specified Spanish stopwords
    stopwords = [' de ', ' la ', ' el ', ' del ', ' las ', ' los ', ' Nuestra Señora ']
    for stopword in stopwords:
        inputstring = inputstring.replace(stopword, ' ')
    inputstring = re.sub(r' +', ' ', inputstring)  # Remove extra spaces
    return inputstring.strip()

def normalize(inputstring):
    # Clean punctuation and apply transformations
    inputstring = re.sub(r'[\.,;:-]', '', str(inputstring))  # Clean punctuation
    inputstring=re.sub(r'á', 'a',  str(inputstring))
    inputstring=re.sub(r'é', 'e',  str(inputstring))
    inputstring=re.sub(r'í', 'i',  str(inputstring))
    inputstring=re.sub(r'ó', 'o',  str(inputstring))
    inputstring=re.sub(r'ú', 'u',  str(inputstring))
    inputstring = re.sub(r'x([aeiouáéíóú])', r'j\1', inputstring)
    inputstring = re.sub(r'X([aeiouáéíóú])', r'J\1', inputstring)
    inputstring = re.sub(r'g([éeií])', r'j\1', inputstring)
    inputstring = re.sub(r'G([éeíi])', r'J\1', inputstring)
    inputstring = re.sub(r'gu', 'hu', inputstring)
    inputstring = re.sub(r'Gu', 'Hu', inputstring)
    inputstring = re.sub(r'v', 'b', inputstring)
    inputstring = re.sub(r'tz', 'z', inputstring)
    inputstring = re.sub(r'y', 'i', inputstring)
    inputstring = re.sub(r'z', 's', inputstring)
    inputstring = re.sub(r's([éeíi])', r'c\1', inputstring)
    inputstring = re.sub(r'ñ', r'n', inputstring)
    inputstring = re.sub(r'Ñ', r'N', inputstring)
    inputstring = re.sub(r'([A-ZÑa-záéíóúüñç-]+)tan\b', r'\1tlan', inputstring)
    inputstring = re.sub(r'([A-ZÑa-záéíóúüñç-]+)cinco\b', r'\1cingo', inputstring)
    inputstring = re.sub(r'([A-ZÑa-záéíóúüñç-]+)zinco\b', r'\1cingo', inputstring)
    inputstring = re.sub(r' +', ' ', inputstring)  # Remove extra spaces
    return inputstring.strip()

def normalize_string(inputstring):
    # Normalize input while keeping the original string unchanged for display
    cleaned_string = stopwordremove(inputstring)
    normalized_string = normalize(cleaned_string)
    return normalized_string

# Helper function to calculate best Levenshtein match
def get_best_levenshtein_match(search_term, target_fields):
    """
    Calculate the best Levenshtein match score for a search term against multiple target fields.
    Returns the highest score found.
    """
    if not search_term or not target_fields:
        return 0
    
    best_score = 0
    search_term_normalized = normalize_string(search_term).lower()
    
    for field in target_fields:
        if field:
            # Compare with both original and normalized versions
            field_normalized = normalize_string(field).lower()
            score1 = fuzz.ratio(search_term_normalized, field.lower())
            score2 = fuzz.ratio(search_term_normalized, field_normalized)
            best_score = max(best_score, score1, score2)
    
    return best_score / 100.0  # Convert to 0-1 scale

def convert_decimal_commas(value):
    if isinstance(value, float):
        return str(value).replace('.', ',')
    if isinstance(value, str):
        try:
            f = float(value)
            return str(f).replace('.', ',')
        except:
            return value
    return value


####################
###### ROUTES ######
####################

# Home route (replaces /discover)
@app.route('/')
def home():
    places_count = len(gz_entidades_csv)
    territories_count = len(entidades_csv)
    officials_count = len(oficiales_csv)

    return render_template(
        'home.html',
        places=places_count,
        territories=territories_count,
        officials=officials_count
    )


##################################
### Detail/Landing page routes ###
##################################

# Route for viewing place details
@app.route('/place/<place_id>')
def place_detail(place_id):
    place = None
    for p in gz_entidades_csv:
        if p['gz_id'] == place_id:
            place = p
            break
    
    if place is None:
        return render_template('404.html'), 404

    church_data = [c for c in church_csv if c['gz_id'] == place_id]
    name_data = [n for n in name_csv if n['gz_id'] == place_id]
    category_data = [ct for ct in category_csv if ct['gz_id'] == place_id]
    geometry_data = [g for g in geometry_csv if g['gz_id'] == place_id]
    cabildo_data = [cb for cb in cabildo_csv if cb['gz_id'] == place_id]
    foreignkeys_data = [f for f in foreignkeys_csv if f['gz_id'] == place_id]
    espartede_data = [i for i in espartede_csv if i['gz_id'] == place_id]
    dependent_data = [d for d in gz_info_csv if d['es_parte_de'] == place_id]
    capital_data = [cp for cp in capital_csv if cp['gz_id'] == place_id]
    institution_data = [inst for inst in institution_csv if inst['gz_id'] == place_id]

    foreignkeys_data = sorted(foreignkeys_data, key=lambda x: x['foreignkey'], reverse=True)
    name_data = sorted(name_data, key=lambda x: x['start'])
    geometry_data = sorted(geometry_data, key=lambda x: x['start'])
    category_data = sorted(category_data, key=lambda x: x['start'])
    cabildo_data = sorted(cabildo_data, key=lambda x: x['start'])
    espartede_data = sorted(espartede_data, key=lambda x: x['overlap_start'])
    dependent_data = sorted(dependent_data, key=lambda x: x['start'])
    capital_data = sorted(capital_data, key=lambda x: (x['Entidad_ID'], x['start']))
    institution_data = sorted(institution_data, key=lambda x: x['START_'])

    article_exists = os.path.exists(os.path.join(app.static_folder, f'articulos/gz_{place_id}.html'))

    coordinates = [{'lat': float(g['lat']), 'lng': float(g['lon'])} for g in geometry_data if 'lat' in g and 'lon' in g]

    return render_template('place_detail.html', place=place, church_data=church_data,
                           name_data=name_data, geometry_data=geometry_data,
                           category_data=category_data, cabildo_data=cabildo_data,
                           foreignkeys_data=foreignkeys_data, espartede_data=espartede_data,
                           dependent_data=dependent_data, capital_data=capital_data,
                           institution_data=institution_data, article_exists=article_exists,
                           coordinates=coordinates)

# Route for viewing territory details
@app.route('/territory/<territory_id>')
def territory_detail(territory_id):
    territory = None
    for t in entidades_csv:
        if t['Entidad'] == territory_id:
            territory = t
            break
    
    if territory is None:
        return render_template('404.html'), 404

    oficiales_entity_data = [o for o in oficiales_entity_csv if o['EntidadID'] == territory_id]
    infotable_data = [inf for inf in infotable_csv if inf['Entidad'] == territory_id]
    fuentes_entidades_data = [f for f in fuentes_entidades_csv if f['entidadID'] == territory_id]
    jerarquia_sup_data = [j for j in jerarquia_csv if j['Entidad'] == territory_id]
    jerarquia_sub_data = [j for j in jerarquia_csv if j['Ent_sup'] == territory_id]
    capital_terr_data = [cp for cp in capital_csv if cp['Entidad_ID'] == territory_id]
    
    oficiales_entity_data = sorted(oficiales_entity_data, key=lambda x: x['Ultima_Fecha_num'])
    infotable_data = sorted(infotable_data, key=lambda x: x['START'])
    jerarquia_sup_data = sorted(jerarquia_sup_data, key=lambda x: x['START'])
    jerarquia_sub_data = sorted(jerarquia_sub_data, key=lambda x: x['START'])
    capital_terr_data = sorted(capital_terr_data, key=lambda x: x['start'])

    fuentes_entidades_grouped = {}
    for fuente in fuentes_entidades_data:
        grupo = fuente['grupo']
        if grupo not in fuentes_entidades_grouped:
            fuentes_entidades_grouped[grupo] = []
        fuentes_entidades_grouped[grupo].append(fuente)
    
    for grupo, fuentes in fuentes_entidades_grouped.items():
        fuentes_entidades_grouped[grupo] = sorted(
            fuentes,
            key=lambda x: (
                x['tiempo'],  # sort lexicographically by tiempo as a string
                'si' != x['foco'],  # custom order: 'si' comes first
                'no' != x['foco'],
                'implicito' != x['foco']
            )
        )


    return render_template('territory_detail.html', territory=territory,
                           oficiales_entity_data=oficiales_entity_data, infotable_data=infotable_data,
                           fuentes_entidades_data=fuentes_entidades_grouped, jerarquia_sup_data=jerarquia_sup_data,
                           jerarquia_sub_data=jerarquia_sub_data, capital_terr_data=capital_terr_data)



# Route for viewing people details
@app.route('/people/persIndias<OficialID>')
def people_detail(OficialID):
    people = None
    for o in oficiales_csv:
        if o.get('OficialID') == OficialID:
            people = o
            break
    
    if people is None:
        return render_template('404.html'), 404

    oficiales_entity_data = [o for o in oficiales_entity_csv if o.get('OficialID') == OficialID]
    oficiales_foreignkeys_data = [f for f in oficiales_foreignkeys_csv if f.get('OficialID') == OficialID]
    oficiales_entity_data = sorted(oficiales_entity_data, key=lambda x: x.get('Ultima_Fecha_num'))

    return render_template('people_detail.html', people=people,
                           oficiales_entity_data=oficiales_entity_data, oficiales_foreignkeys_data=oficiales_foreignkeys_data)


#####################
### Search routes ###
#####################

@app.route('/people/search', methods=['GET', 'POST'])
def people_search():
    # Retrieve and process the last_name_query
    last_name_query = request.args.get('last_name', '').strip()
    year_query = request.args.get('year', type=int)
    fuzzy_search = request.args.get('fuzzy_search') == 'on'

    # Split the last_name_query on "OR" and normalize each term
    last_name_terms = [normalize_string(term.strip()) for term in last_name_query.split('OR')] if last_name_query else []

    def matches_last_name(person, terms, fuzzy=False):
        if not terms:
            return True
            
        # Get searchable field
        searchable_fields = [person.get('Apellidos', '')]
        
        if fuzzy:
            # Use Levenshtein matching with threshold 0.8
            threshold = 0.8
            for term in terms:
                score = get_best_levenshtein_match(term, searchable_fields)
                if score >= threshold:
                    return True
            return False
        else:
            # Word-based matching with wildcard support
            for term in terms:
                apellidos = person.get('Apellidos', '')
                if not apellidos:
                    continue
                
                # Handle wildcard matching
                if '*' in term:
                    # Convert wildcard to regex pattern
                    pattern = term.replace('*', '.*')
                    # Check both original and normalized versions
                    if re.search(r'\b' + pattern + r'\b', apellidos, re.IGNORECASE) or \
                       re.search(r'\b' + pattern + r'\b', normalize_string(apellidos), re.IGNORECASE):
                        return True
                else:
                    # Word boundary matching
                    if re.search(r'\b' + re.escape(term) + r'\b', apellidos, re.IGNORECASE) or \
                       re.search(r'\b' + re.escape(term) + r'\b', normalize_string(apellidos), re.IGNORECASE):
                        return True
            return False

    results = []
    for o in oficiales_csv:
        try:
            range_s = int(float(o.get('RANGE_S', 0)))
            range_e = int(float(o.get('RANGE_E', 0)))
        except ValueError:
            continue

        # Check for matches on any of the last name terms
        last_name_match = matches_last_name(o, last_name_terms, fuzzy_search)
        year_match = (range_s <= year_query < range_e) if year_query else True

        if last_name_match and year_match:
            results.append(o)

    # Sort results by last names for better user experience
    results = sorted(results, key=lambda x: x['Apellidos'].lower())

    return render_template('people_search.html', results=results, last_name=last_name_query, year=year_query, fuzzy_search=fuzzy_search)


@app.route('/territory/search', methods=['GET'])
def territory_search():
    name_query = request.args.get('name', '').strip()
    region = request.args.get('region', '').strip()
    tipo = request.args.get('tipo', '').strip()
    entidad = request.args.get('Entidad', '').strip()
    year_start = request.args.get('year_start', type=int)
    year_end = request.args.get('year_end', type=int)
    year = request.args.get('year', type=int)
    fuzzy_search = request.args.get('fuzzy_search') == 'on'

    if year is not None:
        year_start = year_end = year

    # Split search terms by "OR" and normalize
    name_terms = [normalize_string(term.strip()) for term in name_query.split('OR')] if name_query else []

    regions = sorted({t['Region'] for t in entidades_csv if t['Region']})
    tipos = sorted({t['tipo'] for t in entidades_csv if t['tipo']})
    entidades = sorted({t['Entidad'] for t in entidades_csv if t['Entidad']})

    def matches_territory_name(territory, terms, fuzzy=False):
        if not terms:
            return True
            
        # Get all searchable fields
        searchable_fields = [
            territory['Nombre'],
            territory.get('Variantes', ''),
        ]
        
        if fuzzy:
            # Use Levenshtein matching with threshold 0.8
            threshold = 0.8
            for term in terms:
                score = get_best_levenshtein_match(term, searchable_fields)
                if score >= threshold:
                    return True
            return False
        else:
            # Word-based matching with wildcard support
            for term in terms:
                for field in searchable_fields:
                    if not field:
                        continue
                    
                    # Handle wildcard matching
                    if '*' in term:
                        # Convert wildcard to regex pattern
                        pattern = term.replace('*', '.*')
                        # Check both original and normalized versions
                        if re.search(r'\b' + pattern + r'\b', field, re.IGNORECASE) or \
                           re.search(r'\b' + pattern + r'\b', normalize_string(field), re.IGNORECASE):
                            return True
                    else:
                        # Word boundary matching
                        if re.search(r'\b' + re.escape(term) + r'\b', field, re.IGNORECASE) or \
                           re.search(r'\b' + re.escape(term) + r'\b', normalize_string(field), re.IGNORECASE):
                            return True
            return False

    # Initial match on name/region/tipo/Entidad from entidades_csv
    prefiltered = [
        t for t in entidades_csv if 
        matches_territory_name(t, name_terms, fuzzy_search) and
        (region == '' or t['Region'] == region) and
        (tipo == '' or t['tipo'] == tipo) and
        (entidad == '' or t['Entidad'] == entidad)
    ]

    # If year filtering is active, filter based on infotable_csv using START/END_
    if year_start is not None or year_end is not None:
        # default boundaries
        y_start = year_start if year_start is not None else -9999
        y_end = year_end if year_end is not None else 9999

        # Build a set of Entidad values that have a matching row in infotable_csv
        valid_entidades = set()
        for row in infotable_csv:
            try:
                row_start = int(float(row.get('START', -9999)))
                row_end = int(float(row.get('END_', 9999)))
                if row_end >= y_start and row_start <= y_end:
                    valid_entidades.add(row['Entidad'])
            except (ValueError, TypeError):
                continue

        # Filter only if Entidad is in the valid set
        results = [t for t in prefiltered if t['Entidad'] in valid_entidades]
    else:
        results = prefiltered

    results = sorted(results, key=lambda x: (x['Nombre'].lower(), x['Region'].lower()))

    return render_template(
        'territory_search.html',
        results=results,
        regions=regions,
        tipos=tipos,
        entidades=entidades,
        name_query=name_query,
        selected_region=region,
        selected_tipo=tipo,
        selected_entidad=entidad,
        year_start=year_start,
        year_end=year_end,
        year=year,
        fuzzy_search=fuzzy_search
    )

@app.route('/place/search', methods=['GET', 'POST'])
def place_search():
    # Cache for normalized strings
    normalization_cache = {}
    
    def cached_normalize(text):
        if not text:
            return ''
        if text not in normalization_cache:
            normalization_cache[text] = normalize_string(text)
        return normalization_cache[text]

    # Retrieve all search parameters
    name_query = request.args.get('name', '').strip()
    is_part_of_type = request.args.get('is_part_of_type', '').strip()
    is_part_of_name = request.args.get('is_part_of_name', '').strip()
    year_start = request.args.get('year_start', type=int)
    year_end = request.args.get('year_end', type=int)
    category_query = request.args.get('category', '').strip()
    subcategory_query = request.args.get('subcategory', '').strip()
    church_category = request.args.get('church_category', '').strip()
    religious_order = request.args.get('religious_order', '').strip()
    year = request.args.get('year', type=int)
    fuzzy_search = request.args.get('fuzzy_search') == 'on'

    if year is not None:
        year_start = year_end = year

    # Define church category mappings
    church_category_mappings = {
        'curatos': ['Curato', 'Parroquia', 'Vicaria', 'Sagrario'],
        'misiones': ['Mision capital', 'Mision cabecera', 'Mision'],
        'curatos_misiones': ['Curato', 'Parroquia', 'Vicaria', 'Sagrario', 
                            'Mision capital', 'Mision cabecera', 'Mision'],
        'sede_obispado': ['Sagrario']
    }

    # Get all religious orders for the dropdown
    religious_orders = sorted(list(set(
        info.get('servido_por') for info in gz_info_csv 
        if info.get('servido_por')
    )))

    # Get subcategories for current category if one is selected
    subcategories = []
    if category_query:
        subcategories = sorted(list(set(
            info['categoria_especial'] for info in gz_info_csv 
            if info.get('categoria') == category_query and info.get('categoria_especial')
        )))

    # Split search terms by "OR" and normalize
    name_terms = [cached_normalize(term.strip()) for term in name_query.split('OR')] if name_query else []
    type_terms = [term.strip() for term in is_part_of_type.split('OR')] if is_part_of_type else []
    name_in_territory_terms = [term.strip() for term in is_part_of_name.split('OR')] if is_part_of_name else []

    # Step 1: Initial set of matching GZ IDs based on category, subcategory, church category, and religious order
    matching_gz_ids = set()
    for info in gz_info_csv:
        # Check main category
        if category_query and info.get('categoria') != category_query:
            continue
            
        # Check subcategory if specified
        if subcategory_query and info.get('categoria_especial') != subcategory_query:
            continue

        # Check church category if specified
        if church_category:
            church_types = church_category_mappings.get(church_category, [])
            if info.get('iglesia_cat') not in church_types:
                continue

        # Check religious order if specified
        if religious_order and info.get('servido_por') != religious_order:
            continue

        # Apply temporal filtering to gz_info_csv
        if year_start is not None or year_end is not None:
            try:
                start = int(float(info.get('start', '0'))) if info.get('start') else 0
                end = int(float(info.get('end_', '9999'))) if info.get('end_') else 9999
                if year_start is not None and end < year_start:
                    continue
                if year_end is not None and start > year_end:
                    continue
            except (ValueError, TypeError):
                continue

        matching_gz_ids.add(info['gz_id'])

    # Step 2: Filter by name and variants in gz_entidades
    if name_terms:
        def matches_name_terms(entity, terms, fuzzy=False):
            if not terms:
                return True
                
            # Get all searchable fields
            searchable_fields = [
                entity['nombre'],
                entity['label'],
                entity.get('variantes', ''),
            ]
            
            if fuzzy:
                # Use Levenshtein matching with threshold 0.8
                threshold = 0.8
                for term in terms:
                    score = get_best_levenshtein_match(term, searchable_fields)
                    if score >= threshold:
                        return True
                return False
            else:
                # Word-based matching with wildcard support
                for term in terms:
                    for field in searchable_fields:
                        if not field:
                            continue
                        
                        # Handle wildcard matching
                        if '*' in term:
                            # Convert wildcard to regex pattern
                            pattern = term.replace('*', '.*')
                            # Check both original and normalized versions
                            if re.search(r'\b' + pattern + r'\b', field, re.IGNORECASE) or \
                               re.search(r'\b' + pattern + r'\b', cached_normalize(field), re.IGNORECASE):
                                return True
                        else:
                            # Word boundary matching
                            if re.search(r'\b' + re.escape(term) + r'\b', field, re.IGNORECASE) or \
                               re.search(r'\b' + re.escape(term) + r'\b', cached_normalize(field), re.IGNORECASE):
                                return True
                return False

        filtered_gz_ids = set()
        for entity in gz_entidades_csv:
            if entity['gz_id'] not in matching_gz_ids:
                continue
            
            if matches_name_terms(entity, name_terms, fuzzy_search):
                filtered_gz_ids.add(entity['gz_id'])
        
        matching_gz_ids = filtered_gz_ids

    # Step 3: Apply espartede filters for type and territory name
    if type_terms or name_in_territory_terms:
        filtered_gz_ids = set()
        for part in espartede_csv:
            if part['gz_id'] not in matching_gz_ids:
                continue

            # Apply temporal filtering to espartede data
            if year_start is not None or year_end is not None:
                try:
                    overlap_start = int(float(part.get('overlap_start', '0'))) if part.get('overlap_start') else 0
                    overlap_end = int(float(part.get('overlap_end', '9999'))) if part.get('overlap_end') else 9999
                    if year_start is not None and overlap_end < year_start:
                        continue
                    if year_end is not None and overlap_start > year_end:
                        continue
                except (ValueError, TypeError):
                    continue

            # Check polygon_tipo against type_terms
            type_match = True
            if type_terms:
                type_match = any(term in part.get('polygon_tipo', '') for term in type_terms)

            # Check polygon_label and polygon_variantes against name_in_territory_terms
            label_match = True
            if name_in_territory_terms:
                label_match = False
                # Check polygon_label
                if any(term in part.get('polygon_label', '') for term in name_in_territory_terms):
                    label_match = True
                # Check polygon_variantes (separated by @)
                elif part.get('polygon_variantes'):
                    variants = part['polygon_variantes'].split('@')
                    if any(any(term in variant for term in name_in_territory_terms) for variant in variants):
                        label_match = True

            if type_match and label_match:
                filtered_gz_ids.add(part['gz_id'])
        
        matching_gz_ids = filtered_gz_ids

    # Step 4: Retrieve final results
    results = [
        {
            'gz_id': entity['gz_id'],
            'label': entity['label'],
            'nombre': entity['nombre'],
            'partido_generico': entity.get('partido_generico', 'N/A'),
            'provincia_generica': entity.get('provincia_generica', 'N/A'),
            'region': entity.get('region', 'N/A')
        }
        for entity in gz_entidades_csv if entity['gz_id'] in matching_gz_ids
    ]

    # Sort results
    results = sorted(results, key=lambda x: x['gz_id'].lower())

    return render_template(
        'place_search.html',
        results=results,
        name=name_query,
        is_part_of_type=is_part_of_type,
        is_part_of_name=is_part_of_name,
        year_start=year_start,
        year_end=year_end,
        category=category_query,
        subcategory=subcategory_query,
        subcategories=subcategories,
        church_category=church_category,
        religious_order=religious_order,
        religious_orders=religious_orders,
        fuzzy_search=fuzzy_search
    )



###########################
### Table detail routes ###
###########################
# Route for viewing source details
@app.route('/fuentes/<source_id>')
def source_detail(source_id):
    source_id = source_id.strip().lower()
    source = None
    for s in fuentes_csv:
        csv_source_id = s['FuenteID'].strip().lower()
        if csv_source_id == source_id:
            source = s
            break
    
    if source is None:
        return render_template('404.html'), 404

    return render_template('source_detail.html', source=source)

@app.route('/categoria/<gz_id>')
def category_detail(gz_id):
    place = None
    for p in gz_entidades_csv:
        if p['gz_id'] == gz_id:
            place = p
            break
    
    if place is None:
        return render_template('404.html'), 404

    category_data = [row for row in category_csv if row['gz_id'] == gz_id]
    
    if not category_data:
        return render_template('404.html'), 404

    return render_template('category_detail.html', place=place, category_data=category_data)

#######################
### Download routes ###
#######################
@app.route('/territory/download', methods=['GET'])
def territory_download():
    # Extract search parameters
    name_query = request.args.get('name', '').strip()
    region = request.args.get('region', '').strip()
    tipo = request.args.get('tipo', '').strip()
    entidad = request.args.get('Entidad', '').strip()
    year_start = request.args.get('year_start', type=int)
    year_end = request.args.get('year_end', type=int)
    year = request.args.get('year', type=int)

    if year is not None:
        year_start = year_end = year

    # Normalize and split name terms
    name_terms = [normalize_string(term.strip()) for term in name_query.split('OR')] if name_query else []

    # Initial filter logic from entidades_csv (same as search)
    prefiltered = [
        t for t in entidades_csv if 
        (any(normalize_string(term) in normalize_string(t['Nombre']) or normalize_string(term) in normalize_string(t.get('Variantes', '')) for term in name_terms) if name_terms else True) and
        (region == '' or t['Region'] == region) and
        (tipo == '' or t['tipo'] == tipo) and
        (entidad == '' or t['Entidad'] == entidad)
    ]

    # Apply year filtering if specified
    if year_start is not None or year_end is not None:
        y_start = year_start if year_start is not None else -9999
        y_end = year_end if year_end is not None else 9999

        valid_entidades = set()
        for row in infotable_csv:
            try:
                row_start = int(float(row.get('START', -9999)))
                row_end = int(float(row.get('END_', 9999)))
                if row_end >= y_start and row_start <= y_end:
                    valid_entidades.add(row['Entidad'])
            except (ValueError, TypeError):
                continue

        filtered_results = [t for t in prefiltered if t['Entidad'] in valid_entidades]
    else:
        filtered_results = prefiltered

    # Now build the final output by pulling tipo and Label from infotable_csv
    final_rows = []
    
    for territory in filtered_results:
        entidad_id = territory['Entidad']
        
        # Find matching rows in infotable_csv for this Entidad
        matching_infotable_rows = [
            row for row in infotable_csv 
            if row['Entidad'] == entidad_id
        ]
        
        # If year filtering is active, apply temporal overlap to infotable rows
        if year_start is not None or year_end is not None:
            y_start = year_start if year_start is not None else -9999
            y_end = year_end if year_end is not None else 9999
            
            temporal_matching_rows = []
            for row in matching_infotable_rows:
                try:
                    row_start = int(float(row.get('START', -9999)))
                    row_end = int(float(row.get('END', 9999)))
                    if row_end >= y_start and row_start <= y_end:
                        temporal_matching_rows.append(row)
                except (ValueError, TypeError):
                    continue
            matching_infotable_rows = temporal_matching_rows
        
        # Create rows for each matching infotable entry
        if matching_infotable_rows:
            for info_row in matching_infotable_rows:
                final_rows.append({
                    'Entidad_ID': entidad_id,
                    'tipo': info_row.get('tipo', ''),
                    'Label': info_row.get('Label', ''),  # Changed from 'Nombre'
                    'REG': territory.get('Region', ''),
                    'START': info_row.get('START', ''),
                    'END_': info_row.get('END', ''),
                    'Nivel': territory.get('Nivel_default', '')  # Added Nivel from entidades_csv
                })
        else:
            # If no infotable match found, create a row with empty tipo and Label
            final_rows.append({
                'Entidad_ID': entidad_id,
                'tipo': '',
                'Label': '',
                'REG': territory.get('Region', ''),
                'START': '',
                'END_': '',
                'Nivel': territory.get('Nivel_default', '')
            })

    # Create CSV in memory
    output = StringIO()
    writer = csv.DictWriter(output, fieldnames=['Entidad_ID', 'tipo', 'Label', 'REG', 'START', 'END_', 'Nivel'], delimiter=';')
    writer.writeheader()
    for row in final_rows:
        writer.writerow(row)

    # Prepare response
    response = Response(output.getvalue(), mimetype='text/csv')
    response.headers.set("Content-Disposition", "attachment", filename="territory_results.csv")
    return response


@app.route('/place/download', methods=['GET'])
def place_download():
    normalization_cache = {}

    def cached_normalize(text):
        if not text:
            return ''
        if text not in normalization_cache:
            normalization_cache[text] = normalize_string(text)
        return normalization_cache[text]

    # Retrieve search parameters
    name_query = request.args.get('name', '').strip()
    is_part_of_type = request.args.get('is_part_of_type', '').strip()
    is_part_of_name = request.args.get('is_part_of_name', '').strip()
    year_start = request.args.get('year_start', type=int)
    year_end = request.args.get('year_end', type=int)
    category_query = request.args.get('category', '').strip()
    subcategory_query = request.args.get('subcategory', '').strip()
    church_category = request.args.get('church_category', '').strip()
    religious_order = request.args.get('religious_order', '').strip()

    church_category_mappings = {
        'curatos': ['Curato', 'Parroquia', 'Vicaria', 'Sagrario'],
        'misiones': ['Mision capital', 'Mision cabecera', 'Mision'],
        'curatos_misiones': ['Curato', 'Parroquia', 'Vicaria', 'Sagrario',
                             'Mision capital', 'Mision cabecera', 'Mision'],
        'sede_obispado': ['Sagrario']
    }

    name_terms = [cached_normalize(term.strip()) for term in name_query.split('OR')] if name_query else []
    type_terms = [term.strip() for term in is_part_of_type.split('OR')] if is_part_of_type else []
    name_in_territory_terms = [term.strip() for term in is_part_of_name.split('OR')] if is_part_of_name else []

    # Step 1: Initial filter by category, subcategory, church category, order, and temporal filtering
    matching_gz_ids = set()
    for info in gz_info_csv:
        if category_query and info.get('categoria') != category_query:
            continue
        if subcategory_query and info.get('categoria_especial') != subcategory_query:
            continue
        if church_category:
            if info.get('iglesia_cat') not in church_category_mappings.get(church_category, []):
                continue
        if religious_order and info.get('servido_por') != religious_order:
            continue
        
        # Apply temporal filtering to gz_info_csv
        if year_start is not None or year_end is not None:
            try:
                start = int(float(info.get('start', '0'))) if info.get('start') else 0
                end = int(float(info.get('end_', '9999'))) if info.get('end_') else 9999
                if year_start is not None and end < year_start:
                    continue
                if year_end is not None and start > year_end:
                    continue
            except (ValueError, TypeError):
                continue
        
        matching_gz_ids.add(info['gz_id'])

    # Step 2: Filter by name
    if name_terms:
        name_filtered_ids = set()
        for entity in gz_entidades_csv:
            if entity['gz_id'] not in matching_gz_ids:
                continue
            texts = [
                entity['nombre'].lower(),
                entity['label'].lower(),
                entity.get('variantes', '').lower(),
                cached_normalize(entity['nombre']).lower(),
                cached_normalize(entity['label']).lower(),
                cached_normalize(entity.get('variantes', '')).lower()
            ]
            if any(any(term.lower() in t for t in texts) for term in name_terms):
                name_filtered_ids.add(entity['gz_id'])
        matching_gz_ids = name_filtered_ids

    # Step 3: Filter by espartede data (type and label)
    if type_terms or name_in_territory_terms:
        part_filtered_ids = set()
        for part in espartede_csv:
            if part['gz_id'] not in matching_gz_ids:
                continue

            # Check polygon_tipo against type_terms
            type_match = True
            if type_terms:
                type_match = any(term in part.get('polygon_tipo', '') for term in type_terms)

            # Check polygon_label and polygon_variantes against name_in_territory_terms
            label_match = True
            if name_in_territory_terms:
                label_match = False
                # Check polygon_label
                if any(term in part.get('polygon_label', '') for term in name_in_territory_terms):
                    label_match = True
                # Check polygon_variantes (separated by @)
                elif part.get('polygon_variantes'):
                    variants = part['polygon_variantes'].split('@')
                    if any(any(term in variant for term in name_in_territory_terms) for variant in variants):
                        label_match = True

            if type_match and label_match:
                part_filtered_ids.add(part['gz_id'])

        matching_gz_ids = part_filtered_ids

    # Helper function to convert values with proper data types
    def format_value_with_type(key, value):
        if not value or value == '':
            return ''
            
        # Handle gz_id as int
        if key == 'gz_id':
            try:
                return int(value)
            except (ValueError, TypeError):
                return value
        
        # Handle start and end_ as int
        elif key in ['start', 'end_']:
            try:
                return int(float(value))
            except (ValueError, TypeError):
                return value
        
        # Handle lat and lon as float with comma delimiter
        elif key in ['lat', 'lon']:
            try:
                float_val = float(value)
                return str(float_val).replace('.', ',')
            except (ValueError, TypeError):
                return value
        
        # For other fields, use the original convert_decimal_commas logic
        else:
            return convert_decimal_commas(value)

    # Step 4: Build final data from gz_info_csv for matching gz_ids
    final_rows = []
    for row in gz_info_csv:
        if row.get('gz_id') in matching_gz_ids:
            formatted_row = {}
            for key in ['gz_id', 'label', 'nombre', 'categoria', 'categoria_especial', 
                       'iglesia_cat', 'servido_por', 'start', 'start_ex', 'end_', 
                       'end_ex', 'lat', 'lon', 'cert']:
                formatted_row[key] = format_value_with_type(key, row.get(key, ''))
            final_rows.append(formatted_row)

    # Create CSV with the requested fields
    output = StringIO()
    fieldnames = ['gz_id', 'label', 'nombre', 'categoria', 'categoria_especial', 
                  'iglesia_cat', 'servido_por', 'start', 'start_ex', 'end_', 
                  'end_ex', 'lat', 'lon', 'cert']
    writer = csv.DictWriter(output, fieldnames=fieldnames, delimiter=';')
    writer.writeheader()
    for row in final_rows:
        writer.writerow(row)

    # Return CSV response
    response = Response(output.getvalue(), mimetype='text/csv')
    response.headers.set("Content-Disposition", "attachment", filename="place_results.csv")
    return response


#####################
### Helper routes ###
#####################
@app.route('/get_subcategories/<category>')
def get_subcategories(category):
    # Get unique subcategories for the selected category
    subcategories = sorted(list(set(
        info['categoria_especial'] for info in gz_info_csv 
        if info.get('categoria') == category and info.get('categoria_especial')
    )))
    return jsonify(subcategories)

@app.route('/data/historia_gz_entidades/<path:filename>')
def historia_lugar(filename):
    directory = os.path.join(app.root_path, '..', 'data', 'historia_gz_entidades')
    if os.path.isfile(os.path.join(directory, filename)):
        return send_from_directory(directory, filename)
    else:
        abort(404)
@app.route('/data/historia_entidades/<path:filename>')
def historia_territorio(filename):
    directory = os.path.join(app.root_path, '..', 'data', 'historia_entidades')
    if os.path.isfile(os.path.join(directory, filename)):
        return send_from_directory(directory, filename)
    else:
        abort(404)

if __name__ == '__main__':
    app.run(debug=True)
