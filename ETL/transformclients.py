#!/usr/bin/env python3
# pip install openpyxl pandas
# python ETL/transformclients.py

"""
transformclients.py
-------------------
Takes any Excel file with client/customer data and outputs a clean Excel
formatted for ERP import with columns:
  - Nombre comercial
  - NIF/CIF
  - Dirección completa
  - Teléfono

Usage:
    python transformclients.py
    (a Finder dialog will open to select the input file)
"""

import sys
import re
import unicodedata
from pathlib import Path
import pandas as pd
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter


# ---------------------------------------------------------------------------
# Normalisation helper
# -----------------------------------------
# ----------------------------------

def _norm(s):
    """Lowercase, strip diacritics (á→a, ñ→n, etc.) and whitespace."""
    s = str(s).strip().lower() if s is not None else ""
    return "".join(
        c for c in unicodedata.normalize("NFKD", s)
        if not unicodedata.combining(c)
    )


# ---------------------------------------------------------------------------
# Column detection patterns (matched against normalised header strings)
# ---------------------------------------------------------------------------

NAME_PATTERNS = [
    r"nombre\s*comercial",
    r"nombre\s*(empresa|cliente|social)?",
    r"razon\s*social",
    r"empresa",
    r"company",
    r"customer",
    r"cliente",
    r"^name$",
    r"denominacion",
    r"^nombre$",
]

TAX_PATTERNS = [
    r"nif[\s/]*cif",
    r"cif[\s/]*nif",
    r"^nif$",
    r"^cif$",
    r"^dni$",
    r"tax\s*id",
    r"id\s*fiscal",
    r"identificador\s*fiscal",
    r"vat",
    r"numero\s*fiscal",
    r"fiscal",
]

ADDRESS_PATTERNS = [
    r"direcci[oó]n\s*completa",
    r"direcci[oó]n",
    r"address",
    r"domicilio",
    r"calle",
    r"street",
    r"ubicacion",
    r"localidad",
    r"^dir\.?$",
]

PHONE_PATTERNS = [
    r"tel[eé]fono",
    r"telefono",
    r"^tel\.?$",
    r"phone",
    r"m[oó]vil",
    r"movil",
    r"mobile",
    r"contacto",
    r"^tlf\.?$",
    r"^telf\.?$",
]

# Rows that look like separators/summaries — skip them
SKIP_ROW_PATTERNS = [
    r"^total(es)?$",
    r"^subtotal$",
    r"^\s*$",
]


def _score(header: str, patterns: list) -> int:
    h = _norm(header)
    for pat in patterns:
        if re.search(pat, h):
            return 2 if re.match(pat, h) else 1
    return 0


# ---------------------------------------------------------------------------
# Header-row detection
# ---------------------------------------------------------------------------

def detect_header_row(df: pd.DataFrame) -> int:
    all_patterns = NAME_PATTERNS + TAX_PATTERNS + ADDRESS_PATTERNS + PHONE_PATTERNS
    best_row, best_score = 0, -1

    for i, row in df.iterrows():
        score = sum(
            1 for cell in row.astype(str)
            if any(re.search(p, _norm(cell)) for p in all_patterns)
        )
        if score > best_score:
            best_score, best_row = score, i

    return best_row


# ---------------------------------------------------------------------------
# Column mapping
# ---------------------------------------------------------------------------

def map_columns(headers: list) -> dict:
    targets = {
        "Nombre comercial":   NAME_PATTERNS,
        "NIF/CIF":            TAX_PATTERNS,
        "Dirección completa": ADDRESS_PATTERNS,
        "Teléfono":           PHONE_PATTERNS,
    }
    mapping = {}
    used = set()

    for field, patterns in targets.items():
        best_col, best_score = None, 0
        for i, h in enumerate(headers):
            if i in used:
                continue
            s = _score(h, patterns)
            if s > best_score:
                best_score, best_col = s, i
        if best_col is not None:
            used.add(best_col)
        mapping[field] = best_col

    return mapping


# ---------------------------------------------------------------------------
# Value-type heuristics
# ---------------------------------------------------------------------------

# Spanish/EU tax IDs: 8 digits + letter (DNI), letter + 7 digits + letter (NIE),
# letter + 8 digits (CIF), or generic alphanumeric 7-12 chars
TAX_RE = re.compile(
    r"\b([0-9]{8}[A-Z]|[KLMXYZ][0-9]{7}[A-Z]|[ABCDEFGHJNPQRSUVW][0-9]{7}[0-9A-Z]"
    r"|[A-Z0-9]{7,12})\b",
    re.IGNORECASE,
)

PHONE_RE = re.compile(
    r"\b(\+?[0-9][\d\s\-\.]{6,14}[0-9])\b"
)


def looks_like_name(val: str) -> bool:
    v = val.strip()
    return len(v) > 3 and sum(c.isalpha() for c in v) >= 3


def looks_like_tax_id(val: str) -> bool:
    v = val.strip().replace(" ", "").replace("-", "")
    return bool(TAX_RE.fullmatch(v)) or (6 <= len(v) <= 12 and re.search(r"[A-Z]", v, re.I) and re.search(r"\d", v))


def looks_like_address(val: str) -> bool:
    v = val.strip()
    address_keywords = re.compile(
        r"\b(calle|c/|avda?\.?|avenida|plaza|pz\.?|paseo|po\.?|"
        r"pol[ií]gono|pol\.?|carretera|ctra\.?|camino|bloque|piso|"
        r"street|ave\.?|road|rd\.?|lane|ln\.?|boulevard|blvd\.?)\b",
        re.IGNORECASE,
    )
    if address_keywords.search(v):
        return True
    # Long strings with digits (house number) and text look like addresses
    if len(v) > 10 and re.search(r"\d", v) and sum(c.isalpha() for c in v) > 4:
        return True
    return False


def looks_like_phone(val: str) -> bool:
    v = val.strip()
    # Must be mostly digits (with allowed separators), 7-15 chars total
    digits = re.sub(r"[\s\-\.\+\(\)]", "", v)
    return digits.isdigit() and 7 <= len(digits) <= 15


def _is_skip_row(row_values: list) -> bool:
    non_empty = [str(v).strip() for v in row_values if str(v).strip() not in ("", "nan", "None")]
    if not non_empty:
        return True
    if len(non_empty) <= 2:
        for v in non_empty:
            if any(re.fullmatch(p, _norm(v)) for p in SKIP_ROW_PATTERNS):
                return True
    return False


# ---------------------------------------------------------------------------
# Row extraction
# ---------------------------------------------------------------------------

def _coerce(raw) -> str:
    import math
    if raw is None:
        return ""
    if isinstance(raw, float):
        if math.isnan(raw):
            return ""
        # Phone/tax numbers stored as floats lose leading zeros — restore as int string
        if raw == int(raw):
            return str(int(raw)).strip()
        return str(raw).strip()
    return re.sub(r" +", " ", str(raw).strip())


def smart_fill_row(row_values: list, mapping: dict) -> dict:
    result = {
        "Nombre comercial":   "",
        "NIF/CIF":            "",
        "Dirección completa": "",
        "Teléfono":           "",
    }

    # 1. Fill from mapped columns
    for field, col_idx in mapping.items():
        if col_idx is not None and col_idx < len(row_values):
            v = _coerce(row_values[col_idx])
            if v and v.lower() not in ("nan", "none"):
                if field == "NIF/CIF":
                    v = re.sub(r"[\-\.]", "", v)
                elif field == "Dirección completa":
                    v = v.replace(".", "")
                result[field] = v

    # 2. Heuristic fallback for still-empty fields
    unfilled = [f for f, v in result.items() if not v]
    if unfilled:
        mapped_cols = {idx for idx in mapping.values() if idx is not None}
        for i, raw in enumerate(row_values):
            if i in mapped_cols or not unfilled:
                continue
            v = _coerce(raw)
            if not v or v.lower() in ("nan", "none"):
                continue

            if "Teléfono" in unfilled and looks_like_phone(v):
                result["Teléfono"] = v
                unfilled.remove("Teléfono")
                continue

            if "NIF/CIF" in unfilled and looks_like_tax_id(v):
                result["NIF/CIF"] = re.sub(r"[\-\.]", "", v)
                unfilled.remove("NIF/CIF")
                continue

            if "Dirección completa" in unfilled and looks_like_address(v):
                result["Dirección completa"] = v.replace(".", "")
                unfilled.remove("Dirección completa")
                continue

            if "Nombre comercial" in unfilled and looks_like_name(v):
                result["Nombre comercial"] = v
                unfilled.remove("Nombre comercial")
                continue

    return result


# ---------------------------------------------------------------------------
# Main processing
# ---------------------------------------------------------------------------

def process_excel(input_path: str, output_path: str):
    all_sheets = pd.read_excel(input_path, sheet_name=None, header=None, dtype=str)

    source_df = None
    for sheet_name, df in all_sheets.items():
        if not df.dropna(how="all").empty:
            source_df = df
            print(f"  Using sheet: '{sheet_name}'")
            break

    if source_df is None:
        raise ValueError("No data found in any sheet.")

    source_df = source_df.dropna(how="all").reset_index(drop=True)
    source_df = source_df.dropna(axis=1, how="all")

    header_row_idx = detect_header_row(source_df)
    headers = [str(h).strip() for h in source_df.iloc[header_row_idx].tolist()]
    print(f"  Detected header row {header_row_idx}: {headers}")

    mapping = map_columns(headers)
    print(f"  Column mapping: { {k: (headers[v] if v is not None else None) for k, v in mapping.items()} }")

    data_df = source_df.iloc[header_row_idx + 1:].reset_index(drop=True)

    output_rows = []
    skipped = 0
    for _, row in data_df.iterrows():
        row_vals = row.tolist()

        if _is_skip_row(row_vals):
            skipped += 1
            continue

        rec = smart_fill_row(row_vals, mapping)

        # Skip rows where the client name is missing — minimum required field
        if not rec["Nombre comercial"]:
            skipped += 1
            continue

        output_rows.append(rec)

    print(f"  Extracted {len(output_rows)} client rows  ({skipped} rows skipped)")

    if not output_rows:
        raise ValueError("No client rows could be extracted from the file.")

    # ---------------------------------------------------------------------------
    # Build output Excel
    # ---------------------------------------------------------------------------
    OUTPUT_COLS = [
        "Nombre comercial",
        "NIF/CIF",
        "Dirección completa",
        "Teléfono",
    ]

    wb = Workbook()
    ws = wb.active
    ws.title = "Clientes"

    header_font  = Font(name="Arial", bold=True, color="FFFFFF", size=10)
    header_fill  = PatternFill("solid", start_color="2F5496")
    header_align = Alignment(horizontal="center", vertical="center", wrap_text=True)
    cell_font    = Font(name="Arial", size=10)
    alt_fill     = PatternFill("solid", start_color="EEF2F9")
    thin         = Side(style="thin", color="CCCCCC")
    border       = Border(left=thin, right=thin, top=thin, bottom=thin)
    col_widths   = [40, 16, 55, 18]

    for col_num, (col_name, width) in enumerate(zip(OUTPUT_COLS, col_widths), start=1):
        cell = ws.cell(row=1, column=col_num, value=col_name)
        cell.font      = header_font
        cell.fill      = header_fill
        cell.alignment = header_align
        cell.border    = border
        ws.column_dimensions[get_column_letter(col_num)].width = width
    ws.row_dimensions[1].height = 30

    for row_idx, rec in enumerate(output_rows, start=2):
        fill = alt_fill if row_idx % 2 == 0 else None
        for col_num, col_name in enumerate(OUTPUT_COLS, start=1):
            val = rec[col_name]
            cell        = ws.cell(row=row_idx, column=col_num, value=val)
            cell.font   = cell_font
            cell.border = border
            if fill:
                cell.fill = fill

    ws.freeze_panes = "A2"

    wb.save(output_path)
    print(f"\n✓ Wrote {len(output_rows)} clients → {output_path}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import tkinter as tk
    from tkinter import filedialog, messagebox

    root = tk.Tk()
    root.withdraw()

    input_file = filedialog.askopenfilename(
        title="Select the client Excel file to transform",
        filetypes=[("Excel files", "*.xlsx *.xls"), ("All files", "*.*")],
    )

    if not input_file:
        print("No file selected. Exiting.")
        sys.exit(0)

    p = Path(input_file)
    output_file = str(p.parent / f"{p.stem}_formatted{p.suffix}")

    print(f"Processing: {input_file}")
    try:
        process_excel(input_file, output_file)
        messagebox.showinfo("Done", f"File saved to:\n{output_file}")
    except Exception as e:
        messagebox.showerror("Error", str(e))
        raise
