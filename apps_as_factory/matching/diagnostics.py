import pandas as pd
import re
from rapidfuzz import fuzz, process

def clean_toponym_local(text: str) -> str:
    if pd.isna(text) or not str(text).strip():
        return ''
    text = str(text)
    text = re.sub(r'[\.,;:]', ' ', text)
    replacements = [('á', 'a'), ('Á', 'a'), ('é', 'e'), ('É', 'e'), ('í', 'i'), ('Í', 'i'), ('ó', 'o'), ('Ó', 'o'), ('ú', 'u'), ('Ú', 'u'), ('ñ', 'n'), ('Ñ', 'n')]
    for old, new in replacements:
        text = text.replace(old, new)
    text = text.lower()
    text = text.replace('vv', 'w').replace('v', 'b').replace('tz', 'z').replace('y', 'i').replace('z', 's')
    text = re.sub(r'x([aeiou])', r'j\1', text)
    text = re.sub(r'g([ei])', r'j\1', text)
    text = re.sub(r'gu', 'hu', text)
    text = re.sub(r's([ei])', r'c\1', text)
    text = re.sub(r'\bsta\b', 'santa', text).replace('-', ' ')
    text = re.sub(r'  +', ' ', text)
    text = re.sub(r'\b(de|la|el|del|las|los)\b', ' ', text, flags=re.IGNORECASE)
    text = re.sub(r'  +', ' ', text)
    return text.strip()

# Simulate what the matching code does
input_label = "Puebla"
label_cleaned = clean_toponym_local(input_label)

print(f"Input: '{input_label}' → cleaned: '{label_cleaned}'")
print(f"{'='*80}\n")

# Load data
ref = pd.read_csv("../data/reference_gazetteer.csv", sep=";", encoding="utf-8")
inp = pd.read_csv("../data/uploads/Balmis_cleaned.csv", sep=";", encoding="utf-8")

# Find input row
input_row = inp[inp['ref_Label'].str.lower() == input_label.lower()].iloc[0]
print(f"Input row data:")
print(f"  rowID: {input_row['rowID']}")
print(f"  ref_Label: {input_row['ref_Label']}")
print(f"  ref_Adm0_ISO: {input_row.get('ref_Adm0_ISO', 'N/A')}")
print(f"{'='*80}\n")

# Build index
ref_index = {}
for i, row in ref.iterrows():
    for col in ["lugar_label", "lugar_nombre", "lugar_variantes"]:
        val = clean_toponym_local(str(row.get(col, "")))
        if not val:
            continue
        for token in val.replace("@", " ").split():
            if len(token) < 2:
                continue
            ref_index.setdefault(token, set()).add(i)

# Get candidates
tokens = set(label_cleaned.split())
candidate_ids = set()
for tok in tokens:
    candidate_ids |= ref_index.get(tok, set())

candidates = ref.loc[list(candidate_ids)]
print(f"STEP 1: Token candidates: {len(candidates)}")

# Fuzzy matching
names_cleaned = [clean_toponym_local(str(name)) for name in candidates["lugar_label"]]
matches = process.extract(label_cleaned, names_cleaned, scorer=fuzz.partial_ratio, limit=500, score_cutoff=70)

print(f"STEP 2: Fuzzy matches (≥70): {len(matches)}")
print(f"  Top 5:")
for match_str, score, match_idx in matches[:5]:
    cand = candidates.iloc[match_idx]
    print(f"    Score {score}: gz_id={cand['gz_id']} '{cand['lugar_label']}'")

# Check decide_phase_outcome for these
print(f"\nSTEP 3: Phase decision for each match:")
for match_str, score, match_idx in matches[:5]:
    cand = candidates.iloc[match_idx]
    
    # Simulate the phase decision
    toponym_type = "exact" if score == 100 else "fuzzy"
    saint_status = "no_saint"  # Simplified
    category_status = "category_null"
    
    # Check score thresholds from score.py
    if toponym_type in ("toponym_label", "toponym_nombre") and saint_status in ("saint_match", "no_saint") and category_status in ("category_match", "category_null"):
        phase = "auto_adopt"
    elif score >= 85:
        phase = "candidate"
    else:
        phase = "relegated"
    
    print(f"  gz_id={cand['gz_id']}: score={score/100:.2f} → phase={phase}")

print(f"\nSTEP 4: Checking what should happen:")
print(f"  - Scores ≥0.9 + no_saint + category_null + territory match → auto_adopt")
print(f"  - Scores ≥0.85 → candidate")
print(f"  - Scores <0.85 → relegated")
