# main/matching/cleaning.py
import re
import pandas as pd

def normalize(text: str) -> str:
    """Normalize text for comparison (remove accents, standardize forms)."""
    if pd.isna(text) or not str(text).strip():
        return ''
    text = str(text)
    
    # Remove punctuation first
    text = re.sub(r'[\.,;:]', ' ', text)
    
    # Accent replacements - handle BOTH upper and lowercase
    replacements = [
        ('á', 'a'), ('Á', 'a'),
        ('é', 'e'), ('É', 'e'),
        ('í', 'i'), ('Í', 'i'),
        ('ó', 'o'), ('Ó', 'o'),
        ('ú', 'u'), ('Ú', 'u'),
        ('ñ', 'n'), ('Ñ', 'n')
    ]
    for old, new in replacements:
        text = text.replace(old, new)
    
    # NOW convert to lowercase after accent removal
    text = text.lower()
    
    # Phonetic normalizations (now all lowercase)
    text = text.replace('vv', 'w')
    text = text.replace('v', 'b')  # v→b
    text = text.replace('tz', 'z')
    text = text.replace('y', 'i')  # y→i
    text = text.replace('z', 's')  # z→s
    
    # Regex-based phonetic rules (now all lowercase)
    text = re.sub(r'x([aeiou])', r'j\1', text)  # xa→ja, xe→je, etc.
    text = re.sub(r'g([ei])', r'j\1', text)     # ge→je, gi→ji
    text = re.sub(r'gu', 'hu', text)            # gu→hu
    text = re.sub(r's([ei])', r'c\1', text)     # se→ce, si→ci
    
    # Abbreviation expansions
    text = re.sub(r'\bsta\b', 'santa', text)
    text = re.sub(r'\bsto\b', 'santo', text)
    text = re.sub(r'\bsn\b', 'san', text)
    
    # Clean up
    text = text.replace('-', ' ')
    text = re.sub(r'  +', ' ', text)
    
    return text.strip()

def remove_stopwords(text: str) -> str:
    """Remove Spanish stopwords commonly present in place names."""
    if pd.isna(text) or not text.strip():
        return ''
    text = re.sub(r'\b(de|la|el|del|las|los)\b', ' ', text, flags=re.IGNORECASE)
    text = re.sub(r'nuestra senora', '', text, flags=re.IGNORECASE)
    text = re.sub(r'\bns\b', '', text, flags=re.IGNORECASE)
    text = re.sub(r'  +', ' ', text)
    return text.strip()

def clean_toponym(text: str) -> str:
    """Apply normalization + stopword removal pipeline."""
    if pd.isna(text) or not str(text).strip():
        return ''
    return remove_stopwords(normalize(str(text)))
