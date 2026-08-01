from __future__ import annotations

import csv
from pathlib import Path


CSV_BASENAME = "comentarioshgis"
ID_COLUMN = "EntidadID"
DESCRIPTION_COLUMN = "Descripcion-es"


def find_csv(directory: Path) -> Path:
    """Find comentarioshgis.csv in the script directory, case-insensitively."""
    expected_name = f"{CSV_BASENAME}.csv".lower()

    for path in directory.iterdir():
        if path.is_file() and path.name.lower() == expected_name:
            return path

    raise FileNotFoundError(
        f'Could not find "{CSV_BASENAME}.csv" in:\n{directory}'
    )


def read_csv_text(csv_path: Path) -> tuple[str, str]:
    """Read the CSV and return its text plus the encoding used."""
    encodings = ("utf-8-sig", "utf-8", "cp1252")

    for encoding in encodings:
        try:
            return csv_path.read_text(encoding=encoding), encoding
        except UnicodeDecodeError:
            continue

    raise UnicodeError(
        f"Could not decode {csv_path.name} as UTF-8 or Windows-1252."
    )


def detect_dialect(text: str) -> csv.Dialect:
    """Detect comma, semicolon, or tab-separated CSV data."""
    sample = text[:8192]

    try:
        return csv.Sniffer().sniff(sample, delimiters=",;\t")
    except csv.Error:
        return csv.excel


def validate_filename_id(raw_id: str, row_number: int) -> str:
    """Ensure the ID can safely be used as a Windows filename."""
    file_id = raw_id.strip()

    if not file_id:
        raise ValueError(f"Row {row_number}: empty ID")

    invalid_characters = '<>:"/\\|?*'
    if any(character in file_id for character in invalid_characters):
        raise ValueError(
            f'Row {row_number}: ID "{file_id}" contains an invalid filename character'
        )

    if file_id.endswith((" ", ".")):
        raise ValueError(
            f'Row {row_number}: ID "{file_id}" ends with a space or period'
        )

    return file_id


def main() -> None:
    directory = Path(__file__).resolve().parent
    csv_path = find_csv(directory)

    text, encoding = read_csv_text(csv_path)
    dialect = detect_dialect(text)

    reader = csv.DictReader(text.splitlines(), dialect=dialect)

    if reader.fieldnames is None:
        raise ValueError(f"{csv_path.name} has no header row.")

    missing_columns = [
        column
        for column in (ID_COLUMN, DESCRIPTION_COLUMN)
        if column not in reader.fieldnames
    ]
    if missing_columns:
        available = ", ".join(reader.fieldnames)
        missing = ", ".join(missing_columns)
        raise KeyError(
            f"Missing required column(s): {missing}\n"
            f"Available columns: {available}"
        )

    created = 0
    skipped = 0

    for row_number, row in enumerate(reader, start=2):
        try:
            file_id = validate_filename_id(row.get(ID_COLUMN, ""), row_number)
        except ValueError as error:
            print(f"Skipped: {error}")
            skipped += 1
            continue

        description = row.get(DESCRIPTION_COLUMN) or ""
        output_path = directory / f"{file_id}.txt"
        output_path.write_text(description, encoding="utf-8")
        print(f"Created: {output_path.name}")
        created += 1

    print()
    print(f"CSV: {csv_path.name}")
    print(f"Encoding read: {encoding}")
    print(f"Text files created: {created}")
    print(f"Rows skipped: {skipped}")


if __name__ == "__main__":
    main()
