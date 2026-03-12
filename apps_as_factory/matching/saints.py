# main/matching/saints.py
import re
import pandas as pd
from typing import Optional, List

def load_saints(filepath: str) -> List[str]:
    """Load list of saints from santos.csv file and normalize them."""
    try:
        df = pd.read_csv(filepath, sep=';', encoding='utf-8')
        saints_raw = df['santo'].dropna().unique().tolist()
        
        # Normalize saints using the same cleaning logic
        from .cleaning import clean_toponym
        saints = [clean_toponym(s) for s in saints_raw if s]
        saints = [s for s in saints if s]  # Remove empty strings
        saints.sort(key=len, reverse=True)  # Longest first to match greedily
        return saints
    except Exception as e:
        print(f"⚠️ Could not load saints list: {e}")
        return []

def extract_saint(text: str, saints_list: List[str]) -> Optional[str]:
    """Return first matching saint name from a text."""
    if pd.isna(text) or not str(text).strip():
        return None
    lower_text = text.lower()
    for saint in saints_list:
        # Saints are already normalized/cleaned
        if saint.lower() in lower_text:
            return saint.lower()
    return None

def compare_saints(s1: Optional[str], s2: Optional[str]) -> str:
    """Compare extracted saint names."""
    if not s1 or not s2:
        return "no_saint"
    if s1 == s2 or s1 in s2 or s2 in s1:
        return "saint_match"
    return "saint_mismatch"
