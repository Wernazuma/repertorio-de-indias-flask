import pandas as pd
import os

def chrono_match(inputname: str):
    base_path = "../data"
    cleaned_path = os.path.join(base_path, f"uploads/{inputname}_cleaned.csv")
    processing_path = os.path.join(base_path, f"uploads/{inputname}_processing.csv")
    gz_info_path = os.path.join(base_path, "gz_info_1.csv")
    output_path = os.path.join(base_path, f"uploads/{inputname}_matched.csv")

    # Load all CSVs
    df_cleaned = pd.read_csv(cleaned_path, sep=";", decimal=",", dtype=str)
    df_proc = pd.read_csv(processing_path, sep=";", decimal=",", dtype=str)
    df_gz = pd.read_csv(gz_info_path, sep=";", decimal=",", dtype=str)

    # Normalize types
    df_proc["rowID"] = df_proc["rowID"].astype(str)
    df_cleaned["rowID"] = df_cleaned["rowID"].astype(str)
    df_cleaned["ref_START"] = pd.to_numeric(df_cleaned["ref_START"], errors="coerce")
    df_cleaned["ref_END"] = pd.to_numeric(df_cleaned["ref_END"], errors="coerce")
    df_gz["start"] = pd.to_numeric(df_gz["start"], errors="coerce")
    df_gz["end_"] = pd.to_numeric(df_gz["end_"], errors="coerce")

    # Ensure columns exist
    for col in ["gz_id", "lat", "lon"]:
        if col not in df_cleaned.columns:
            df_cleaned[col] = None

    matched_rows = []

    for _, row in df_cleaned.iterrows():
        row_id = row["rowID"]
        ref_START = row["ref_START"]
        ref_END = row["ref_END"]

        # Get matching row from processing
        proc_row = df_proc[df_proc["rowID"] == row_id]
        if not proc_row.empty:
            gz_id = proc_row.iloc[0].get("gz_id")
            row["gz_id"] = gz_id
            row["lat"] = proc_row.iloc[0].get("lat", "0")
            row["lon"] = proc_row.iloc[0].get("lon", "0")
        else:
            gz_id = None
            row["gz_id"] = "0"
            row["lat"] = "0"
            row["lon"] = "0"

        # No date or no valid gz_id: keep as-is
        if pd.isna(ref_START) or pd.isna(ref_END) or not gz_id:
            matched_rows.append(row)
            continue

        # Filter gz_info to matching gz_id only!
        gz_subset = df_gz[df_gz["gz_id"] == gz_id]

        # Find overlapping periods
        overlaps = gz_subset[
            (gz_subset["start"] <= ref_END) & (gz_subset["end_"] >= ref_START)
        ].sort_values(by="start")

        if overlaps.empty:
            matched_rows.append(row)
            continue

        for _, gz_row in overlaps.iterrows():
            seg_start = max(ref_START, gz_row["start"])
            seg_end = min(ref_END, gz_row["end_"])

            new_row = row.copy()
            new_row["ref_START"] = int(seg_start)
            new_row["ref_END"] = int(seg_end)
            new_row["lat"] = gz_row.get("lat", "0")
            new_row["lon"] = gz_row.get("lon", "0")
            new_row["gz_id"] = gz_row.get("gz_id", row.get("gz_id"))

            matched_rows.append(new_row)

    # Save result
    df_result = pd.DataFrame(matched_rows)
    df_result.to_csv(output_path, sep=";", index=False, decimal=",")
    print(f"✅ Saved to: {output_path}")
    return output_path


# --- 🔧 Change this value only ---
if __name__ == "__main__":
    inputname = "Balmis"  # ← Change this to your input name (prefix)
    chrono_match(inputname)
