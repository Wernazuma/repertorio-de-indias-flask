#!/usr/bin/env python3
"""
Diagnostic script to debug row 85 getting stuck.
Run this from your project root directory.
"""

import os
import pandas as pd
import sys

# Adjust these paths to match your setup
UPLOAD_FOLDER = os.path.join("data", "uploads")
GAZETTEER_FILE = os.path.join("data", "espartede.csv")

def diagnose_row(prefix, row_number):
    """Diagnose a specific row that's causing issues"""
    
    input_path = os.path.join(UPLOAD_FOLDER, f"{prefix}_cleaned.csv")
    
    print(f"\n{'='*80}")
    print(f"DIAGNOSING ROW {row_number} FROM {prefix}")
    print(f"{'='*80}\n")
    
    # Load input data
    try:
        input_df = pd.read_csv(input_path, delimiter=';', decimal=',', encoding='utf-8')
        print(f"✓ Loaded input file: {len(input_df)} rows")
    except Exception as e:
        print(f"✗ ERROR loading input file: {e}")
        return
    
    # Get the specific row
    if row_number >= len(input_df):
        print(f"✗ ERROR: Row {row_number} doesn't exist (only {len(input_df)} rows)")
        return
    
    row = input_df.iloc[row_number]
    print(f"\n--- ROW {row_number} DATA ---")
    print(f"rowID: {row.get('rowID', 'N/A')}")
    print(f"ref_Label: {row.get('ref_Label', 'N/A')}")
    
    # Show all ref_ fields
    ref_fields = [col for col in row.index if col.startswith('ref_')]
    print(f"\n--- REFERENCE FIELDS ({len(ref_fields)}) ---")
    for field in ref_fields:
        value = row.get(field)
        if pd.notna(value) and str(value).strip() != "":
            print(f"  {field}: {value}")
    
    # Load gazetteer
    try:
        gazetteer_df = pd.read_csv(GAZETTEER_FILE, delimiter=';', decimal=',', encoding='utf-8')
        print(f"\n✓ Loaded gazetteer: {len(gazetteer_df)} entries")
    except Exception as e:
        print(f"\n✗ ERROR loading gazetteer: {e}")
        return
    
    # Simulate filtering
    print(f"\n--- SIMULATING FILTERING ---")
    filtered = gazetteer_df.copy()
    print(f"Starting size: {len(filtered)}")
    
    # Generic territory filters
    generic_filters = {
        "ref_Partido_generico": "lugar_partido_generico",
        "ref_Provincia_generica": "lugar_provincia_generica",
        "ref_Region": "lugar_region",
        "ref_Pais": "lugar_pais"
    }
    
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
    
    for ref_field, gz_field in generic_filters.items():
        value = row.get(ref_field)
        if pd.notna(value) and str(value).strip() != "":
            if ref_field == "ref_Region":
                value = REGION_CODE_TO_NAME.get(value, value)
            
            if gz_field in filtered.columns:
                before = len(filtered)
                filtered = filtered[filtered[gz_field] == value]
                after = len(filtered)
                print(f"  {ref_field}={value}: {before} → {after}")
    
    # Time filtering
    year = None
    if pd.notna(row.get("ref_Year")):
        year = int(row["ref_Year"])
    elif pd.notna(row.get("ref_START")) and pd.notna(row.get("ref_END")):
        year = (int(row["ref_START"]) + int(row["ref_END"])) // 2
    
    if year:
        year = min(max(year, 1701), 1808)
        if 'overlap_start' in filtered.columns and 'overlap_end' in filtered.columns:
            before = len(filtered)
            filtered = filtered[
                (filtered['overlap_start'] <= year) & (filtered['overlap_end'] >= year)
            ]
            after = len(filtered)
            print(f"  Time filter (year={year}): {before} → {after}")
    
    print(f"\nAfter initial filtering: {len(filtered)} entries remain")
    
    if filtered.empty:
        print("\n⚠ WARNING: No gazetteer entries remain after filtering!")
        print("This might cause the matcher to behave unexpectedly.")
        return
    
    # Check hierarchical levels
    print(f"\n--- HIERARCHICAL LEVEL CHECK ---")
    
    levels = [
        {
            "name": "partido/jurisdiccion",
            "fields": ["ref_Partido", "ref_Jurisdiccion"]
        },
        {
            "name": "provincia",
            "fields": ["ref_Provincia", "ref_Provincia_menor"]
        },
        {
            "name": "provincia_mayor/obispado",
            "fields": ["ref_Provincia_mayor", "ref_Obispado"]
        },
        {
            "name": "audiencia",
            "fields": ["ref_Audiencia"]
        }
    ]
    
    for level in levels:
        has_data = any(
            pd.notna(row.get(f)) and str(row.get(f)).strip() != ""
            for f in level["fields"]
        )
        
        if has_data:
            print(f"\n  Level: {level['name']}")
            for field in level["fields"]:
                value = row.get(field)
                if pd.notna(value) and str(value).strip() != "":
                    print(f"    {field}: {value}")
        else:
            print(f"\n  Level: {level['name']} - NO DATA")
    
    # Check for potential problematic patterns
    print(f"\n--- POTENTIAL ISSUES ---")
    
    ref_label = row.get("ref_Label", "")
    if not ref_label or pd.isna(ref_label):
        print("⚠ ref_Label is empty or NaN!")
    
    # Check if any polygon columns exist
    polygon_cols = ["polygon_label", "polygon_nombre", "polygon_variantes"]
    missing_cols = [col for col in polygon_cols if col not in gazetteer_df.columns]
    if missing_cols:
        print(f"⚠ Missing polygon columns in gazetteer: {missing_cols}")
    
    # Check for very large filtered sets
    if len(filtered) > 1000:
        print(f"⚠ Large filtered set ({len(filtered)} rows) might cause slow matching")
    
    print(f"\n{'='*80}\n")


if __name__ == "__main__":
    print("\n" + "="*80)
    print("ROW DIAGNOSTIC TOOL")
    print("="*80 + "\n")
    
    # Prompt for prefix
    prefix = input("Enter the dataset prefix (e.g., 'mydata'): ").strip()
    if not prefix:
        print("✗ ERROR: Prefix cannot be empty")
        sys.exit(1)
    
    # Prompt for row number
    row_input = input("Enter the row number to diagnose (default: 85): ").strip()
    if row_input:
        try:
            row_number = int(row_input)
        except ValueError:
            print(f"✗ ERROR: '{row_input}' is not a valid number")
            sys.exit(1)
    else:
        row_number = 85
    
    diagnose_row(prefix, row_number)
