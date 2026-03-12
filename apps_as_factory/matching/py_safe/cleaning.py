# main/matching/cleaning.py
import re
import pandas as pd

def normalize(text: str) -> str:
    """Normalize text for comparison (remove accents, standardize forms)."""
    if pd.isna(text) or not str(text).strip():
        return ''
    text = str(text)
    text = re.sub(r'[\.,;:]', ' ', text)
    replacements = [
        ('á', 'a'), ('é', 'e'), ('í', 'i'), ('ó', 'o'), ('ú', 'u'),
        ('vv', 'w'), ('v', 'b'), ('tz', 'z'), ('y', 'i'), ('z', 's'), ('ñ', 'n')
    ]
    for old, new in replacements:
        text = text.replace(old, new)
    text = re.sub(r'x([aeiou])', r'j\1', text)
    text = re.sub(r'g([ei])', r'j\1', text)
    text = re.sub(r'gu', 'hu', text)
    text = re.sub(r's([ei])', r'c\1', text)
    text = re.sub(r'(Sta )', 'Santa ', text)
    text = re.sub(r'(Sto )', 'Santo ', text)
    text = re.sub(r'(Sn )', 'San ', text)
    text = re.sub(r'-', ' ', text)
    text = re.sub(r'  +', ' ', text)
    return text.lower().strip()

def remove_stopwords(text: str) -> str:
    """Remove Spanish stopwords commonly present in place names."""
    if pd.isna(text) or not text.strip():
        return ''
    text = re.sub(r'\b(de|la|el|del|las|los)\b', ' ', text, flags=re.IGNORECASE)
    text = re.sub(r'Nuestra Señora', '', text, flags=re.IGNORECASE)
    text = re.sub(r'NS', '', text, flags=re.IGNORECASE)
    text = re.sub(r'  +', ' ', text)
    return text.strip()

def clean_toponym(text: str) -> str:
    """Apply normalization + stopword removal pipeline."""
    if pd.isna(text) or not str(text).strip():
        return ''
    return remove_stopwords(normalize(str(text)))
