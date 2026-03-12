# diagnose_specific_missing.py
# Check why specific gz_ids are not appearing in results

import pandas as pd
import os
import re
from rapidfuzz import process, fuzz

def clean_toponym_local(text: str) -> str:
    """Local copy of clean_toponym."""
    if pd.isna(text) or not str(text).strip():
        return ''
    
    text = str(text)
    text = re.sub(r'[\.,;:]', ' ', text)
    
    replacements = [
        ('á', 'a'), ('Á', 'a'), ('é', 'e'), ('É', 'e'),
        ('í', 'i'), ('Í', 'i'), ('ó', 'o'), ('Ó', 'o'),
        ('ú', 'u'), ('Ú', 'u'), ('ñ', 'n'), ('Ñ', 'n')
    ]
    for old, new in replacements:
        text = text.replace(old, new)
    
    text = text.lower()
    text = text.replace('vv', 'w')
    text = text.replace('v', 'b')
    text = text.replace('tz', 'z')
    text = text.replace('y', 'i')
    text = text.replace('z', 's')
    
    text = re.sub(r'x([aeiou])', r'j\1', text)
    text = re.sub(r'g([ei])', r'j\1', text)
    text = re.sub(r'gu', 'hu', text)
    text = re.sub(r's([ei])', r'c\1', text)
    text = re.sub(r'\bsta\b', 'santa', text)
    text = re.sub(r'\bsto\b', 'santo', text)
    text = re.sub(r'\bsn\b', 'san', text)
    text = text.replace('-', ' ')
    text = re.sub(r'  +', ' ', text)
    
    text = re.sub(r'\b(de|la|el|del|las|los)\b', ' ', text, flags=re.IGNORECASE)
    text = re.sub(r'nuestra senora', '', text, flags=re.IGNORECASE)
    text = re.sub(r'\bns\b', '', text, flags=re.IGNORECASE)
    text = re.sub(r'  +', ' ', text)
    
    return text.strip()

# Test cases: (search_term, expected_gz_id, input_adm0)
test_cases = [
    ("Concepción", 7000002, "CHL"),
    ("San Juan", 3000003, "PRI"),
    ("San Gil", 5000042, "COL"),
]

# Load reference
possible_paths = [
    "../../data/reference_gazetteer.csv",
    "../data/reference_gazetteer.csv",
    "data/reference_gazetteer.csv",
]

ref_path = None
for path in possible_paths:
    if os.path.exists(path):
        ref_path = path
        break

if not ref_path:
    print("ERROR: Could not find reference_gazetteer.csv")
    exit(1)

ref = pd.read_csv(ref_path, sep=";", encoding="utf-8")

print("=" * 80)
print("DIAGNOSING MISSING MATCHES")
print("=" * 80)

for search_term, expected_gz, input_adm0 in test_cases:
    print(f"\n{'='*80}")
    print(f"🔍 Searching for: '{search_term}' (expecting gz_id={expected_gz} in {input_adm0})")
    print(f"{'='*80}")
    
    # Check if expected gz_id exists
    expected_record = ref[ref['gz_id'] == expected_gz]
    if expected_record.empty:
        print(f"❌ gz_id={expected_gz} NOT FOUND in reference!")
        continue
    
    expected_record = expected_record.iloc[0]
    print(f"\n✅ Target record exists:")
    print(f"   lugar_label: '{expected_record['lugar_label']}'")
    print(f"   lugar_nombre: '{expected_record.get('lugar_nombre', 'N/A')}'")
    print(f"   adm0_iso: '{expected_record.get('adm0_iso', 'N/A')}'")
    
    # Clean search term
    search_cleaned = clean_toponym_local(search_term)
    print(f"\n📝 Search term cleaned: '{search_term}' -> '{search_cleaned}'")
    
    # Clean expected record fields
    expected_label_clean = clean_toponym_local(str(expected_record['lugar_label']))
    expected_nombre_clean = clean_toponym_local(str(expected_record.get('lugar_nombre', '')))
    print(f"   Target label cleaned: '{expected_label_clean}'")
    print(f"   Target nombre cleaned: '{expected_nombre_clean}'")
    
    # Build index
    index = {}
    for i, row in ref.iterrows():
        for col in ["lugar_label", "lugar_nombre", "lugar_variantes"]:
            val = clean_toponym_local(str(row.get(col, "")))
            if not val:
                continue
            for token in val.replace("@", " ").split():
                if len(token) < 2:
                    continue
                index.setdefault(token, set()).add(i)
    
    # Get candidates via token matching
    tokens = set(search_cleaned.split())
    candidate_ids = set()
    for tok in tokens:
        candidate_ids |= index.get(tok, set())
    
    print(f"\n🔎 Token-based candidates: {len(candidate_ids)}")
    
    # Check if expected record is in candidates
    expected_idx = expected_record.name
    if expected_idx in candidate_ids:
        print(f"   ✅ Target gz_id={expected_gz} IS in token candidates")
    else:
        print(f"   ❌ Target gz_id={expected_gz} NOT in token candidates")
        print(f"      Tokens from search: {tokens}")
        print(f"      Target should have been indexed with tokens from:")
        print(f"        '{expected_label_clean}' -> {expected_label_clean.split()}")
        print(f"        '{expected_nombre_clean}' -> {expected_nombre_clean.split()}")
        continue
    
    # Get all candidates
    candidates = ref.loc[list(candidate_ids)]
    
    # Fuzzy matching
    names = candidates["lugar_label"].fillna("").tolist()
    matches = process.extract(
        search_cleaned,
        names,
        scorer=fuzz.partial_ratio,
        limit=20,
        score_cutoff=70
    )
    
    print(f"\n📊 Fuzzy matches (limit=20, cutoff=70): {len(matches)}")
    
    # Check if expected record made it through fuzzy matching
    expected_in_fuzzy = False
    expected_score = None
    for match_str, score, match_idx in matches:
        cand = candidates.iloc[match_idx]
        if cand['gz_id'] == expected_gz:
            expected_in_fuzzy = True
            expected_score = score
            print(f"   ✅ Target gz_id={expected_gz} passed fuzzy (score={score})")
            break
    
    if not expected_in_fuzzy:
        # Check what score it actually got
        expected_label_in_candidates = expected_record['lugar_label']
        actual_score = fuzz.partial_ratio(search_cleaned, expected_label_in_candidates)
        print(f"   ❌ Target gz_id={expected_gz} FAILED fuzzy matching")
        print(f"      Actual score: {actual_score}")
        print(f"      Comparing: '{search_cleaned}' vs '{expected_label_in_candidates}'")
        
        if actual_score < 70:
            print(f"      REASON: Score {actual_score} < 70 (cutoff)")
        else:
            print(f"      REASON: Score {actual_score} >= 70 but not in top 20 results")
            print(f"      Top 5 matches:")
            for i, (match_str, score, _) in enumerate(matches[:5], 1):
                print(f"        {i}. '{match_str}' (score={score})")
        continue
    
    # Check deduplication (shouldn't affect anything but let's verify)
    print(f"\n🔄 After deduplication:")
    unique_gz = set()
    for match_str, score, match_idx in matches:
        cand = candidates.iloc[match_idx]
        unique_gz.add(cand['gz_id'])
    
    if expected_gz in unique_gz:
        print(f"   ✅ Target gz_id={expected_gz} survived deduplication")
    else:
        print(f"   ❌ Target gz_id={expected_gz} lost in deduplication (shouldn't happen)")
        continue
    
    # Check territorial filtering
    print(f"\n🗺️  Territorial filtering check:")
    print(f"   Input has Adm0_ISO: {input_adm0}")
    print(f"   Target record adm0_iso: '{expected_record.get('adm0_iso', 'N/A')}'")
    
    target_matches_territory = (
        str(expected_record.get('adm0_iso', '')).strip().lower() == input_adm0.lower()
    )
    
    if target_matches_territory:
        print(f"   ✅ Target MATCHES input territory")
    else:
        print(f"   ❌ Target DOES NOT match input territory")
        print(f"      This could be why it's filtered out if other candidates do match")
    
    # Check which candidates match territory
    matching_territory = []
    for match_str, score, match_idx in matches:
        cand = candidates.iloc[match_idx]
        if str(cand.get('adm0_iso', '')).strip().lower() == input_adm0.lower():
            matching_territory.append(cand['gz_id'])
    
    print(f"   Candidates matching territory: {len(matching_territory)}")
    if len(matching_territory) > 0 and expected_gz not in matching_territory:
        print(f"   ⚠️  OTHER candidates match territory, so target would be filtered out")
        print(f"      Matching gz_ids: {matching_territory[:5]}")

print(f"\n{'='*80}")
