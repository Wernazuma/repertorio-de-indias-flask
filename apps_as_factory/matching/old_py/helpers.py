import pandas as pd
import re
from fuzzywuzzy import fuzz
import os

def normalize(inputstring):
    if pd.isna(inputstring) or inputstring == '':
        return ''
    inputstring = re.sub(r'[\.,;:]', ' ', str(inputstring))
    replacements = [
        ('á', 'a'), ('é', 'e'), ('í', 'i'), ('ó', 'o'), ('ú', 'u'),
        ('vv', 'w'), ('v', 'b'), ('tz', 'z'), ('y', 'i'), ('z', 's'), ('ñ', 'n')
    ]
    for old, new in replacements:
        inputstring = re.sub(old, new, inputstring)
    inputstring = re.sub(r'x([aeiouáéíóú])', r'j\1', inputstring)
    inputstring = re.sub(r'g([éeií])', r'j\1', inputstring)
    inputstring = re.sub(r'gu', 'hu', inputstring)
    inputstring = re.sub(r's([éeíi])', r'c\1', inputstring)
    inputstring = re.sub(r'([^c])h([aeiouáéíóú])([^eaéá])', r'\1\2\3', inputstring)
    inputstring = re.sub(r'(Sta )', 'Santa ', inputstring)
    inputstring = re.sub(r'(Sto )', 'Santo ', inputstring)
    inputstring = re.sub(r'(Sn )', 'San ', inputstring)
    inputstring = inputstring.lower()
    inputstring = re.sub(r'  +', ' ', inputstring)
    inputstring = re.sub(r'-', ' ', inputstring)
    return inputstring.strip()

def stopwordremove(inputstring):
    if pd.isna(inputstring) or inputstring == '':
        return ''
    inputstring = re.sub(r'(?<!\w)(de|la|el|del|las|los)(?!\w)', ' ', inputstring, flags=re.IGNORECASE)
    inputstring = re.sub(r'Nuestra Señora', '', inputstring, flags=re.IGNORECASE)
    inputstring = re.sub(r'NS', '', inputstring)
    inputstring = re.sub(r'  +', ' ', inputstring)
    return inputstring.strip()

def clean_toponym(toponym):
    if pd.isna(toponym) or toponym == '':
        return ''
    return stopwordremove(normalize(str(toponym)))

def load_patron_saints():
    try:
        santos_file = os.path.join(r"../data/", "santos.csv")
        santos_df = pd.read_csv(santos_file, delimiter=';', encoding='utf-8')
        saints = santos_df['santo'].dropna().unique().tolist()
        saints.sort(key=len, reverse=True)
        return saints
    except Exception as e:
        print(f"Error loading santos.csv: {e}")
        return []
