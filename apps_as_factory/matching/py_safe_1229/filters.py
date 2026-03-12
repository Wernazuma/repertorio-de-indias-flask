# main/matching/filters.py
import pandas as pd
from typing import Optional

def prefilter_by_territory(ref_df: pd.DataFrame, row: pd.Series) -> pd.DataFrame:
    """
    Dynamically filter the reference gazetteer using any available
    territorial information in the input row.
    """
    mapping = {
        "ref_Adm0_ISO": "adm0_iso",
        "ref_Adm0": "adm0_label",
        "ref_Region": "lugar_region",
        "ref_Audiencia": "audiencia_label",
        "ref_Provincia": "provincia_label",
        "ref_Provincia_mayor": "provincia_mayor_label",
        "ref_Provincia_menor": "provincia_menor_label",
        "ref_Jurisdiccion": "jurisdiccion_label",
        "ref_Partido": "partido_label",
    }

    filters = []
    for input_field, ref_field in mapping.items():
        if input_field in row.index and ref_field in ref_df.columns:
            val = row.get(input_field)
            if pd.notna(val) and str(val).strip():
                filters.append(ref_df[ref_field] == str(val).strip())

    if not filters:
        return ref_df

    combined = filters[0]
    for f in filters[1:]:
        combined &= f

    filtered = ref_df[combined]
    return filtered if not filtered.empty else ref_df


def compare_category(ref_category: Optional[str],
                     lugar_categoria: Optional[str],
                     lugar_categoria_especial: Optional[str]) -> str:
    """
    Compare input and reference category fields.
    """
    if not ref_category or pd.isna(ref_category) or not str(ref_category).strip():
        return "category_null"

    if ref_category in (lugar_categoria, lugar_categoria_especial):
        return "category_match"

    return "category_mismatch"


def ensure_dtype_consistency(ref_df: pd.DataFrame) -> pd.DataFrame:
    """
    Normalize dtypes: ensure float/int consistency.
    Only lat/lon should remain float, others become str or int.
    """
    numeric_exceptions = ["lat", "lon"]
    for col in ref_df.columns:
        if col in numeric_exceptions:
            ref_df[col] = pd.to_numeric(ref_df[col], errors="coerce")
        elif ref_df[col].dtype in ["float64", "int64"]:
            ref_df[col] = ref_df[col].astype("Int64", errors="ignore")
        else:
            ref_df[col] = ref_df[col].astype(str)
    return ref_df
