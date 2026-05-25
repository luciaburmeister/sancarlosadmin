#!/usr/bin/env python3
# pip install openpyxl pandas
# python ETL/transformquotes.py

"""
transformquotes.py
------------------
Takes any Excel file with sales/quote data and outputs a clean Excel
formatted for ERP import with columns:
  - Número de documento
  - Fecha
  - Nombre de cliente
  - Total
  - Estado

Usage: python transformquotes.py
(a Finder dialog will open to select the input file)
"""

import sys
import re
import unicodedata
from pathlib import Path
from datetime import datetime

import pandas as pd
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter

EXCEL_EPOCH = datetime(1899, 12, 30)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _norm(s):
    """Lowercase + strip diacritics (á→a, ñ→n, etc.)."""
    s = str(s).strip().lower() if s is not None else ""
    return "".join(
        c for c in unicodedata.normalize("NFKD", s)
        if not unicodedata.combining(c)
    )


def _coerce(raw) -> str:
    """Convert any cell value to a plain string."""
    if raw is None:
        return ""
    if isinstance(raw, float):
        import math
        if math.isnan(raw):
            return ""
        if raw == int(raw):
            return str(int(raw))
        return str(raw)
    return str(raw).strip()


# ---------------------------------------------------------------------------
# Column header patterns
# ---------------------------------------------------------------------------

DOCUMENT_PATTERNS = [
    r"num(ero)?\s*(de\s*)?doc(umento)?",
    r"doc(umento)?\s*num",
    r"num(ero)?\s*presupuesto",
    r"presupuesto",
    r"num(ero)?\s*(pedido|albaran|factura|ref)",
    r"pedido|albaran|factura|referencia",
    r"invoice",
    r"^ref$",
    r"^num(ero)?$",
    r"^n[º°]",
    r"^id$",
    r"^doc$",
    r"^codigo$",
    r"^folio$",
]

DATE_PATTERNS = [
    r"fecha",
    r"date",
    r"emision",
]

CLIENT_PATTERNS = [
    r"num(ero)?\s*(de\s*)?cliente",
    r"^cliente$",
    r"^client$",
    r"^nombre$",
    r"nombre\s*(cliente|empresa|comercial)",
    r"razon\s*social",
    r"^empresa$",
    r"^company$",
    r"^customer$",
    r"destinatario",
]

TOTAL_PATTERNS = [
    r"^total$",
    r"importe\s*total",
    r"precio\s*total",
    r"total\s*con\s*iva",
    r"total\s*factura",
    r"^importe$",
    r"^precio$",
    r"^amount$",
    r"^value$",
    r"^valor$",
    r"^pvp$",
]

STATUS_PATTERNS = [
    r"^estado$",
    r"estado\s*(presupuesto|oferta|pedido)?",
    r"^status$",
    r"^situacion$",
    r"^resultado$",
    r"^respuesta$",
]

ALL_PATTERNS = (
    DOCUMENT_PATTERNS + DATE_PATTERNS + CLIENT_PATTERNS
    + TOTAL_PATTERNS + STATUS_PATTERNS
)


def _score(header: str, patterns: list) -> int:
    h = _norm(header)
    for pat in patterns:
        if re.search(pat, h):
            return 2 if re.match(pat, h) else 1
    return 0


# ---------------------------------------------------------------------------
# Status value normalisation
# ---------------------------------------------------------------------------

STATUS_MAP = [
    (r"no\s*acept|rechaz|perdi|cancel|^no$|^ko$",  "No aceptado"),
    (r"acept|aprob|ganado|confirm|^si$|^yes$|^ok$", "Aceptado"),
    (r"ofert|envi|pendiente|espera|^draft$|^sent$",  "Ofertado"),
]


def _normalise_status(val: str) -> str:
    v = _norm(val)
    for pattern, label in STATUS_MAP:
        if re.search(pattern, v):
            return label
    return val.strip().title() if val.strip() else ""


# ---------------------------------------------------------------------------
# Header-row detection
# ---------------------------------------------------------------------------

def detect_header_row(df: pd.DataFrame) -> int:
    """
    Find the row most likely to be the real header.
    - Skips rows with fewer than 2 non-empty cells (title rows).
    - Scores by how many distinct target field groups have a match.
    - Falls back to row 0 only if nothing better is found.
    """
    target_groups = [
        DOCUMENT_PATTERNS,
        DATE_PATTERNS,
        CLIENT_PATTERNS,
        TOTAL_PATTERNS,
        STATUS_PATTERNS,
    ]
    best_row, best_score = None, -1
    for i, row in df.iterrows():
        cells = [_norm(c) for c in row.astype(str)]
        non_empty = [c for c in cells if c and c != "nan"]
        if len(non_empty) < 2:          # skip title / single-value rows
            continue
        score = sum(
            1 for group in target_groups
            if any(re.search(p, cell) for cell in cells for p in group)
        )
        if score > best_score:
            best_score, best_row = score, i
    return best_row if best_row is not None else 0


# ---------------------------------------------------------------------------
# Column mapping  (header name → output field)
# ---------------------------------------------------------------------------

def map_columns(headers: list) -> dict:
    """Return {output_field: source_col_index | None} using header names."""
    targets = {
        "Número de documento": DOCUMENT_PATTERNS,
        "Fecha":               DATE_PATTERNS,
        "Nombre de cliente":   CLIENT_PATTERNS,
        "Total":               TOTAL_PATTERNS,
        "Estado":              STATUS_PATTERNS,
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


DATE_RE  = re.compile(r"\d{1,4}[/\-\.]\d{1,2}[/\-\.]\d{2,4}")
MONEY_RE = re.compile(r"^[\d\s\.,]+€?$")
STATUS_WORDS = re.compile(
    r"acept|no\s*acept|ofert|rechaz|pendiente|envi|perdi|ganado|cancel",
    re.IGNORECASE,
)
DOC_RE = re.compile(
    r"^\d{3,}$"                          # pure digits: 001, 1234
    r"|^[A-Z]{1,6}[\-/]?\d{2,}"          # letters then digits: MET-1, FAC2025
    r"|^\d{4}[A-Z]{1,6}[\-/]?\d+"        # year + letters + number: 2025MET-1
    r"|^\d{4}[\-/]\d{1,5}$",             # year-short number: 2025-1 (not 2025-11-27)
    re.IGNORECASE,
)


def map_columns_from_data(data_df: pd.DataFrame) -> dict:
    """
    Fallback: inspect the first 30 data rows per column and vote on what
    type of data each column contains.
    Returns {output_field: col_index | None}.
    """
    sample = data_df.head(30)
    n_cols = len(sample.columns)

    votes = {
        "Fecha":               [0] * n_cols,
        "Estado":              [0] * n_cols,
        "Total":               [0] * n_cols,
        "Número de documento": [0] * n_cols,
        "Nombre de cliente":   [0] * n_cols,
    }

    for col_i in range(n_cols):
        for raw in sample.iloc[:, col_i]:
            v = _coerce(raw)
            if not v or v.lower() in ("nan", "none"):
                continue
            # Date
            if DATE_RE.search(v) or _parse_excel_serial(v):
                votes["Fecha"][col_i] += 3
            # Status word
            if STATUS_WORDS.search(v):
                votes["Estado"][col_i] += 3
            # Money/total: numeric with optional decimal, not a pure 4-digit year/id
            cleaned = v.replace(" ", "").replace(",", ".").replace("€", "")
            try:
                num = float(cleaned)
                if not (1000 <= num <= 9999 and cleaned == str(int(num))):
                    votes["Total"][col_i] += 2
            except ValueError:
                pass
            # Document code — exclude values that look like dates
            if DOC_RE.match(v.strip()) and not DATE_RE.search(v):
                votes["Número de documento"][col_i] += 3
            # Client name: mostly letters, length > 4
            if len(v) > 4 and sum(c.isalpha() for c in v) > len(v) * 0.5:
                votes["Nombre de cliente"][col_i] += 1

    mapping = {}
    used = set()
    # Assign in priority order
    for field in ["Fecha", "Estado", "Total", "Número de documento", "Nombre de cliente"]:
        scores = [
            (votes[field][i], i) for i in range(n_cols) if i not in used
        ]
        if not scores:
            mapping[field] = None
            continue
        best_score, best_col = max(scores)
        if best_score > 0:
            mapping[field] = best_col
            used.add(best_col)
        else:
            mapping[field] = None
    return mapping


# ---------------------------------------------------------------------------
# Date helpers
# ---------------------------------------------------------------------------

def _parse_excel_serial(val: str) -> str | None:
    try:
        n = float(val)
        if 20000 < n < 60000:
            d = EXCEL_EPOCH + __import__("datetime").timedelta(days=int(n))
            return d.strftime("%d/%m/%Y")
    except (ValueError, TypeError):
        pass
    return None


def _clean_date(val: str) -> str:
    """Strip time portion and convert Excel serials."""
    serial = _parse_excel_serial(val)
    if serial:
        return serial
    return val.split(" ")[0].split("T")[0].strip()


# ---------------------------------------------------------------------------
# Client name cleaning
# ---------------------------------------------------------------------------

def _clean_client(val: str) -> str:
    """Remove leading numeric ID, e.g. '1234 - ACME S.L.' → 'ACME S.L.'"""
    v = val.strip()
    m = re.match(r"^\d+\s*[-–]\s*(.+)$", v)
    if m:
        return m.group(1).strip()
    m = re.match(r"^(.+?)\s*[-–]\s*\d+\s*$", v)
    if m:
        return m.group(1).strip()
    return v


# ---------------------------------------------------------------------------
# Row builder  (mapping-only, no heuristics)
# ---------------------------------------------------------------------------

def build_row(row_values: list, mapping: dict) -> dict:
    result = {
        "Número de documento": "",
        "Fecha":               "",
        "Nombre de cliente":   "",
        "Total":               "",
        "Estado":              "",
    }
    for field, col_idx in mapping.items():
        if col_idx is None or col_idx >= len(row_values):
            continue
        v = _coerce(row_values[col_idx])
        if not v or v.lower() in ("nan", "none"):
            continue
        if field == "Fecha":
            v = _clean_date(v)
        elif field == "Nombre de cliente":
            v = _clean_client(v)
        elif field == "Estado":
            v = _normalise_status(v)
        result[field] = v

    if not result["Estado"]:
        result["Estado"] = "Borrador"

    return result


def _is_empty_row(row_values: list) -> bool:
    return all(_coerce(v) in ("", "nan", "none") for v in row_values)


# ---------------------------------------------------------------------------
# Main processing
# ---------------------------------------------------------------------------

def process_excel(input_path: str, output_path: str):
    all_sheets = pd.read_excel(input_path, sheet_name=None, header=None, dtype=str)

    source_df = None
    for sheet_name, df in all_sheets.items():
        if not df.dropna(how="all").empty:
            source_df = df
            print(f"  Sheet: '{sheet_name}'")
            break

    if source_df is None:
        raise ValueError("No data found in any sheet.")

    source_df = source_df.dropna(how="all").reset_index(drop=True)
    source_df = source_df.dropna(axis=1, how="all")

    header_row_idx = detect_header_row(source_df)
    headers = [str(h).strip() for h in source_df.iloc[header_row_idx].tolist()]

    mapping = map_columns(headers)
    data_df = source_df.iloc[header_row_idx + 1:].reset_index(drop=True)

    # If fewer than 2 fields were mapped by headers, fall back to data scanning
    found = sum(1 for v in mapping.values() if v is not None)
    if found < 2:
        print("  Header mapping insufficient — detecting columns from data values...")
        mapping = map_columns_from_data(data_df)

    # Diagnostics
    print(f"  Headers found:  {headers}")
    mapping_info = {k: (f"col {v} -> {headers[v] if v < len(headers) else '?'}" if v is not None else "NOT FOUND") for k, v in mapping.items()}
    print(f"  Column mapping: {mapping_info}")

    output_rows = []
    for _, row in data_df.iterrows():
        row_vals = row.tolist()
        if _is_empty_row(row_vals):
            continue
        rec = build_row(row_vals, mapping)
        if not rec["Número de documento"]:
            continue
        output_rows.append(rec)

    if not output_rows:
        raise ValueError("No data rows could be extracted from the file.")

    # Preview
    print(f"\n  {len(output_rows)} rows extracted. Preview (first 3):")
    for rec in output_rows[:3]:
        print(f"    {rec}")
    print()

    # ---------------------------------------------------------------------------
    # Write output Excel
    # ---------------------------------------------------------------------------
    OUTPUT_COLS = [
        "Número de documento",
        "Fecha",
        "Nombre de cliente",
        "Total",
        "Estado",
    ]

    wb = Workbook()
    ws = wb.active
    ws.title = "Presupuestos"

    header_font  = Font(name="Arial", bold=True, color="FFFFFF", size=10)
    header_fill  = PatternFill("solid", start_color="2F5496")
    header_align = Alignment(horizontal="center", vertical="center", wrap_text=True)
    cell_font    = Font(name="Arial", size=10)
    alt_fill     = PatternFill("solid", start_color="EEF2F9")
    thin         = Side(style="thin", color="CCCCCC")
    border       = Border(left=thin, right=thin, top=thin, bottom=thin)
    col_widths   = [22, 14, 45, 14, 16]

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
            if col_name == "Total" and val:
                try:
                    val = float(
                        val.replace(" ", "").replace("€", "")
                           .replace("$", "").replace(",", ".")
                    )
                except (ValueError, AttributeError):
                    pass
            cell        = ws.cell(row=row_idx, column=col_num, value=val)
            cell.font   = cell_font
            cell.border = border
            if fill:
                cell.fill = fill
            if col_name == "Total" and isinstance(val, float):
                cell.number_format = "0.00"
            elif col_name == "Fecha":
                cell.alignment = Alignment(horizontal="center")

    ws.freeze_panes = "A2"
    wb.save(output_path)
    print(f"✓ Saved → {output_path}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import tkinter as tk
    from tkinter import filedialog, messagebox

    root = tk.Tk()
    root.withdraw()

    input_file = filedialog.askopenfilename(
        title="Select the Excel file to transform",
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
