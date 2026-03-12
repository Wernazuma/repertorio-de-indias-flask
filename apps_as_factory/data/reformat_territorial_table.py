
import pandas as pd
import numpy as np

def normalize_value(val):
    if pd.isna(val):
        return ''
    if isinstance(val, float):
        return f"{val:.8f}"
    return str(val).strip()

def rows_equal(row1, row2, exclude_keys):
    for key in row1.keys():
        if key in exclude_keys:
            continue
        v1 = normalize_value(row1.get(key, ''))
        v2 = normalize_value(row2.get(key, ''))
        if v1 != v2:
            return False
    return True

def reformat_territorial_table(input_csv, output_csv):
    df = pd.read_csv(input_csv, sep=';')

    # Normalize Nivel
    df['Nivel'] = df['Nivel'].str.strip().str.lower()

    # Define levels and polygon fields to extract
    niveles = ['jurisdiccion', 'provincia', 'provincia_menor', 'provincia_mayor',
               'partido', 'audiencia', 'obispado']
    polygon_fields = ['label', 'nombre', 'variantes', 'entidad_id']

    # Basic lugar fields to preserve
    lugar_fields = [
        'gz_id', 'lugar_label', 'lugar_nombre', 'lugar_variantes',
        'lugar_categoria', 'lugar_categoria_especial', 'lugar_iglesia_cat',
        'lugar_partido_generico', 'lugar_provincia_generica', 'lugar_region',
        'lat', 'lon', 'lugar_adm0_iso', 'lugar_adm0', 'lugar_adm1', 'lugar_adm2'
    ]

    output_rows = []

    for gz_id, group in df.groupby('gz_id'):
        base_info = group.iloc[0][lugar_fields].to_dict()

        # Build time intervals from all starts and ends
        boundaries = sorted(set(group['overlap_start']).union(set(group['overlap_end'])))
        intervals = [(boundaries[i], boundaries[i + 1] - 1) for i in range(len(boundaries) - 1)]

        for start, end in intervals:
            row = base_info.copy()
            row['overlap_start'] = start
            row['overlap_end'] = end

            for nivel in niveles:
                nivel_rows = group[group['Nivel'] == nivel]
                match = nivel_rows[
                    (nivel_rows['overlap_start'] <= start) &
                    (nivel_rows['overlap_end'] >= end)
                ]
                if not match.empty:
                    selected = match.iloc[0]
                    for f in polygon_fields:
                        row[f'{nivel}_{f}'] = selected[f'polygon_{f}']
            output_rows.append(row)

    df_out = pd.DataFrame(output_rows)
    df_out = df_out.sort_values(by=['gz_id', 'overlap_start'])

    # Merge consecutive rows
    merged_rows = []
    prev_row = None
    for _, row in df_out.iterrows():
        current = row.to_dict()
        if prev_row is None:
            prev_row = current
            continue

        if (
            prev_row['gz_id'] == current['gz_id'] and
            prev_row['overlap_end'] + 1 == current['overlap_start'] and
            rows_equal(prev_row, current, exclude_keys=['overlap_start', 'overlap_end'])
        ):
            prev_row['overlap_end'] = current['overlap_end']
        else:
            merged_rows.append(prev_row)
            prev_row = current

    if prev_row:
        merged_rows.append(prev_row)

    df_merged = pd.DataFrame(merged_rows)
    df_merged = df_merged.sort_values(by=['gz_id', 'overlap_start'])

    # Output with ; separator and , as decimal
    df_merged.to_csv(output_csv, sep=';', decimal=',', index=False)

# Example usage:
# reformat_territorial_table("example.csv", "reformatted_output.csv")




reformat_territorial_table("espartede.csv", "reformatted_output.csv")
