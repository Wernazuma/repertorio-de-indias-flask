import os
import pandas as pd

# Region code (input, e.g. "CHA") -> region name as spelled in the gazetteer
# (e.g. "Charcas"). Loaded lazily from gz_entidades so the name matches the
# gazetteer's lugar_region exactly (including any source encoding quirks).
_REGION_CODE_TO_NAME = None


def _load_region_map() -> dict:
    global _REGION_CODE_TO_NAME
    if _REGION_CODE_TO_NAME is not None:
        return _REGION_CODE_TO_NAME
    mapping = {}
    path = os.path.join("data", "gz_entidades.csv")
    try:
        # Column "region" holds the 3-letter code, "region_full" the gazetteer
        # region name (e.g. region=CHA -> region_full=Charcas).
        e = pd.read_csv(path, sep=";", encoding="utf-8", low_memory=False,
                        usecols=["region", "region_full"])
        for reg, region in zip(e["region"], e["region_full"]):
            if pd.notna(reg) and pd.notna(region):
                code = str(reg).strip().upper()
                name = str(region).strip()
                if code and name and code not in mapping:
                    mapping[code] = name
    except Exception:
        pass
    _REGION_CODE_TO_NAME = mapping
    return mapping


def region_code_to_name(code: str):
    """Translate an input region code to the gazetteer's region name, or None."""
    if not code:
        return None
    return _load_region_map().get(str(code).strip().upper())


_REGION_NAME_TO_CODE = None


def _load_region_name_map() -> dict:
    """Inverse of the code->name map: verbose region name (lower-cased) -> code."""
    global _REGION_NAME_TO_CODE
    if _REGION_NAME_TO_CODE is not None:
        return _REGION_NAME_TO_CODE
    inv = {}
    for code, name in _load_region_map().items():
        inv.setdefault(name.strip().lower(), code)
    _REGION_NAME_TO_CODE = inv
    return inv


def region_to_code(value: str):
    """Canonicalise a region value to its 3-letter CODE, accepting EITHER a code
    ("CHA") or the verbose name ("Charcas"). Returns the upper-case code, or None
    if the value matches neither. Used so region comparisons work no matter which
    representation the input or the gazetteer carries (the gazetteer's
    lugar_region is currently the code, but has historically been the name)."""
    if not value:
        return None
    s = str(value).strip()
    if not s:
        return None
    up = s.upper()
    if up in _load_region_map():          # already a known code
        return up
    return _load_region_name_map().get(s.lower())  # a known verbose name


REGION_NEIGHBORS = {
    "CHL": {"RPL"},
    "RPL": {"CHL", "CHA"},
    "CHA": {"RPL", "PER"},
    "PER": {"CHA", "QUI"},
    "QUI": {"PER", "NGR"},
    "NGR": {"QUI", "TFI", "VEN"},
    "TFI": {"NGR"},
    "VEN": {"NGR"},
    "SDO": set(),
    "GUA": {"NES"},
    "NES": {"GUA", "GDJ"},
    "GDJ": {"NES"},
    "FIL": set(),
    "EXT": set(),
}


def regions_compatible(region_a: str, region_b: str) -> bool:
    """Return True if two region codes are the same or geographical neighbors."""
    if not region_a or not region_b:
        return False
    a, b = region_a.strip().upper(), region_b.strip().upper()
    if a == b:
        return True
    return b in REGION_NEIGHBORS.get(a, set())
