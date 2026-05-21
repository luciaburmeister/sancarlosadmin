#!/usr/bin/env python3
# pip install openpyxl pandas
# python ETL/transformquoteitems.py

"""
transformquoteitems.py
----------------------
Takes any Excel file with quote line-item data and outputs a clean Excel
formatted for ERP import with columns:

  - Número de documento   (quote / project name — links the item to its quote)
  - Codigo Articulo       (unique article / material code)
  - Nombre articulo       (article description)
  - Cliente               (client name — informational, not used for linking)
  - Precio                (unit price)

The input file can have columns in any order and any of the common Spanish /
English header variants.  The script detects them automatically.

Usage:
    python ETL/transformquoteitems.py
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
# Helpers
# ---------------------------------------------------------------------------

def _norm(s) -> str:
    """Lowercase + strip diacritics (á→a, ñ→n, …) + strip whitespace."""
    s = str(s).strip().lower() if s is not None else ""
    return "".join(
        c for c in unicodedata.normalize("NFKD", s)
        if not unicodedata.combining(c)
    )


def _coerce(raw) -> str:
    """Convert any cell value to a plain string, handling NaN and floats."""
    if raw is None:
        return ""
    if isinstance(raw, float):
        import math
        if math.isnan(raw):
            return ""
        return str(int(raw)) if raw == int(raw) else str(raw)
    return str(raw).strip()


def _clean_money(val: str) -> str:
    """Return a plain decimal string, stripping currency symbols and spaces."""
    v = val.replace("€", "").replace("$", "").replace(" ", "").strip()
    # Spanish format: "1.234,56" → "1234.56"
    if "," in v and "." in v:
        if v.rfind(",") > v.rfind("."):
            v = v.replace(".", "").replace(",", ".")
        else:
            v = v.replace(",", "")
    elif "," in v:
        v = v.replace(",", ".")
    try:
        return str(float(v))
    except ValueError:
        return val


# ---------------------------------------------------------------------------
# Column header patterns
# Used to auto-detect which source column maps to which output field.
# ---------------------------------------------------------------------------

DOC_PATTERNS = [
    # Most specific: explicit "presupuesto" references (quote numbers)
    r"presupuesto\s+inicial",          # "Presupuesto Inicial" — common export column
    r"num[\s\-_.]*(ero)?[\s\-_.]*presupuesto",  # Núm-Presupuesto, Numero Presupuesto
    r"^presupuesto$",
    r"n[uú]m[\s\-_.]*(ero)?[\s\-_.]*(de[\s\-_.]+)?presupuesto",
    r"^n[º°][\s\-_.]*(doc|presupuesto)",
    # Generic document number — but NOT factura/invoice (those are invoices, not quotes)
    r"num[\s\-_.]*(ero)?[\s\-_.]*(de[\s\-_.]+)?doc(umento)?",
    r"doc(umento)?[\s\-_.]*num",
    r"num[\s\-_.]*(ero)?[\s\-_.]*(pedido|ref)",
    r"^folio$",
    r"^id[\s\-_.]*(doc|quote|presupuesto)?$",
    r"^num(ero)?$",
]

ARTICLE_CODE_PATTERNS = [
    r"cod[\s\-_.]*(igo)?[\s\-_.]*(de[\s\-_.]+)?art(iculo)?",  # Cod-Artículo, Codigo Articulo, …
    r"^referencia$",
    r"^ref\.?$",
    r"^codigo$",
    r"^code$",
    r"^item[\s\-]*code$",
    r"^article[\s\-]*code$",
    r"^sku$",
    r"^part[\s\-]*(number|no|num)?$",
]

ARTICLE_NAME_PATTERNS = [
    r"nombre\s*(de\s*)?art(iculo)?",
    r"^descripcion$",
    r"^description$",
    r"^detalle$",
    r"^concepto$",
    r"^art(iculo)?$",
    r"^nombre$",
    r"^item\s*(name|description)?$",
]

CLIENT_PATTERNS = [
    r"^cliente$",
    r"^client$",
    r"^customer$",
    r"nombre\s*(cliente|empresa|comercial)",
    r"^empresa$",
    r"^company$",
    r"razon\s*social",
    r"destinatario",
]

PRICE_PATTERNS = [
    r"^precio(\s*unitario)?$",
    r"precio\s*(unit|ud|unidad)?",
    r"^price$",
    r"^unit\s*price$",
    r"^importe$",
    r"^pvp$",
    r"^valor$",
    r"^amount$",
    r"^total$",
]

DISCOUNT_PATTERNS = [
    r"^descuento$",
    r"^discount$",
    r"^dto\.?$",
    r"^desc\.?$",
    r"^rebaja$",
    r"descuento\s*(%|porcentaje)?",
]

COST_PATTERNS = [
    r"^coste(\s*unitario)?$",
    r"^costo(\s*unitario)?$",
    r"precio\s*(de\s*)?coste",
    r"^cost(\s*per\s*unit)?$",
    r"^coste[\s\-_/]*ud(s?|\.)?$",
    r"^purchase\s*price$",
    r"^buying\s*price$",
]

FAMILY_PATTERNS = [
    r"descripcion\s*familia",
    r"desc[\s\-_.]*familia",
    r"^familia$",
    r"^family$",
    r"grupo\s*(de\s*)?art(iculo)?",
    r"^grupo$",
    r"^subfamilia$",
]

ALL_GROUPS = [
    DOC_PATTERNS,
    ARTICLE_CODE_PATTERNS,
    ARTICLE_NAME_PATTERNS,
    CLIENT_PATTERNS,
    PRICE_PATTERNS,
    DISCOUNT_PATTERNS,
    COST_PATTERNS,
    FAMILY_PATTERNS,
]


def _score(header: str, patterns: list) -> int:
    """
    Score a header against a list of patterns.
    Patterns listed earlier are more specific and score higher:
      - Match on pattern i  → base (2 or 1) + (len(patterns) - i)
    This ensures "Núm-Presupuesto" (matches pattern 1) always beats a
    generic "Número" column (matches fallback pattern 8).
    """
    h = _norm(header)
    for i, pat in enumerate(patterns):
        if re.search(pat, h):
            base = 2 if re.match(pat, h) else 1
            specificity = len(patterns) - i   # earlier patterns are worth more
            return base + specificity
    return 0


# ---------------------------------------------------------------------------
# Header-row detection
# ---------------------------------------------------------------------------

def detect_header_row(df: pd.DataFrame) -> int:
    """
    Return the index of the row most likely to be the real header row.
    Scores each row by how many distinct target field groups it matches.
    """
    best_row, best_score = 0, -1
    for i, row in df.iterrows():
        cells = [_norm(c) for c in row.astype(str)]
        non_empty = [c for c in cells if c and c != "nan"]
        if len(non_empty) < 2:
            continue
        score = sum(
            1 for group in ALL_GROUPS
            if any(re.search(p, cell) for cell in cells for p in group)
        )
        if score > best_score:
            best_score, best_row = score, i
    return best_row


# ---------------------------------------------------------------------------
# Column mapping
# ---------------------------------------------------------------------------

def map_columns(headers: list, verbose: bool = True) -> dict:
    """
    Return {output_field: source_column_index | None} matched by header name.
    Prints a per-column score table when verbose=True.
    """
    targets = {
        "Número de documento":  DOC_PATTERNS,
        "Codigo Articulo":      ARTICLE_CODE_PATTERNS,
        "Nombre articulo":      ARTICLE_NAME_PATTERNS,
        "Cliente":              CLIENT_PATTERNS,
        "Precio":               PRICE_PATTERNS,
        "Descuento":            DISCOUNT_PATTERNS,
        "Coste unitario":       COST_PATTERNS,
        "Descripción Familia":  FAMILY_PATTERNS,
    }

    if verbose:
        field_names = list(targets.keys())
        print("\n  ── Column score table ──────────────────────────────────────────")
        header_line = f"  {'Col':<4} {'Raw header':<35} {'Normalized':<30}"
        for fn in field_names:
            header_line += f"  {fn[:12]:<12}"
        print(header_line)
        print("  " + "-" * (4 + 35 + 30 + len(field_names) * 14 + 2))
        for i, h in enumerate(headers):
            scores = {fn: _score(h, pats) for fn, pats in targets.items()}
            row = f"  {i:<4} {h:<35} {_norm(h):<30}"
            for fn in field_names:
                row += f"  {scores[fn]:<12}"
            print(row)
        print()

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
# Row builder
# ---------------------------------------------------------------------------

def _clean_client(val: str) -> str:
    """Strip leading numeric ID: '1234 - ACME S.L.' → 'ACME S.L.'"""
    v = val.strip()
    m = re.match(r"^\d+\s*[-–]\s*(.+)$", v)
    if m:
        return m.group(1).strip()
    m = re.match(r"^(.+?)\s*[-–]\s*\d+\s*$", v)
    if m:
        return m.group(1).strip()
    return v


def _clean_discount(val: str) -> str:
    """
    Return a percentage string (0–100 scale).

    Source files may store discounts as:
      - A percentage already: "15", "15%", "15,00"  → "15.0"
      - A decimal fraction:   "0.15", "0,15"        → "15.0"

    Rule: if the parsed number is > 1 it is already a percentage; if ≤ 1
    it is a decimal fraction and gets multiplied by 100.
    An empty/zero discount is stored as "0.0".
    """
    v = val.replace("%", "").replace(" ", "").strip()
    # Spanish decimal comma
    if "," in v and "." not in v:
        v = v.replace(",", ".")
    elif "," in v and ".":
        # e.g. "1.000,50" — treat the comma as decimal separator
        if v.rfind(",") > v.rfind("."):
            v = v.replace(".", "").replace(",", ".")
        else:
            v = v.replace(",", "")
    try:
        num = float(v)
    except ValueError:
        return ""
    # Decimal fraction → convert to percentage
    if 0 < num <= 1:
        num = num * 100
    return str(round(num, 4))


def build_row(row_values: list, mapping: dict) -> dict:
    result = {
        "Número de documento": "",
        "Codigo Articulo":     "",
        "Nombre articulo":     "",
        "Cliente":             "",
        "Precio":              "",
        "Descuento":           "",
        "Coste unitario":      "",
        "Descripción Familia": "",
        "Total neto":          "",
        "Margen (%)":          "",
    }
    for field, col_idx in mapping.items():
        if col_idx is None or col_idx >= len(row_values):
            continue
        v = _coerce(row_values[col_idx])
        if not v or v.lower() in ("nan", "none"):
            continue
        if field == "Cliente":
            v = _clean_client(v)
        elif field in ("Precio", "Coste unitario"):
            v = _clean_money(v)
        elif field == "Descuento":
            v = _clean_discount(v)
        result[field] = v

    # Compute net total: Precio × (1 − Descuento/100)
    try:
        precio = float(result["Precio"]) if result["Precio"] else None
        disc   = float(result["Descuento"]) if result["Descuento"] else 0.0
        if precio is not None:
            result["Total neto"] = str(round(precio * (1 - disc / 100), 4))
    except (ValueError, ZeroDivisionError):
        pass

    _fill_margin(result)
    return result


def _fill_margin(rec: dict):
    """(Re)compute Margen (%) from Total neto and Coste unitario in-place."""
    rec["Margen (%)"] = ""
    try:
        net  = float(rec["Total neto"])      if rec["Total neto"]      else None
        cost = float(rec["Coste unitario"])  if rec["Coste unitario"]  else None
        if net is not None and cost is not None and net != 0:
            rec["Margen (%)"] = str(round((net - cost) / net * 100, 2))
    except (ValueError, ZeroDivisionError):
        pass


def _is_empty_row(row_values: list) -> bool:
    return all(_coerce(v) in ("", "nan", "none") for v in row_values)


# ---------------------------------------------------------------------------
# Cost file loader  (optional second Excel: item code → unit cost)
# ---------------------------------------------------------------------------

def load_cost_map(cost_path: str) -> dict:
    """
    Read a separate Excel file that maps article codes to unit costs.
    Returns {normalised_code: cost_string}.

    The file can have any layout — columns are auto-detected using the same
    ARTICLE_CODE_PATTERNS and COST_PATTERNS as the main file.
    """
    print(f"\n  Loading cost file: {cost_path}")
    all_sheets = pd.read_excel(cost_path, sheet_name=None, header=None, dtype=str)

    df = None
    for sheet_name, sheet_df in all_sheets.items():
        if not sheet_df.dropna(how="all").empty:
            df = sheet_df.dropna(how="all").reset_index(drop=True)
            df = df.dropna(axis=1, how="all")
            print(f"  Sheet: '{sheet_name}'")
            break

    if df is None:
        print("  WARNING: Cost file is empty — skipping.")
        return {}

    header_row_idx = detect_header_row(df)
    headers = [str(h).strip() for h in df.iloc[header_row_idx].tolist()]

    # Pick the best-scoring code, cost, name and family columns independently
    code_col,   code_best   = None, 0
    cost_col,   cost_best   = None, 0
    name_col,   name_best   = None, 0
    family_col, family_best = None, 0
    for i, h in enumerate(headers):
        cs = _score(h, ARTICLE_CODE_PATTERNS)
        if cs > code_best:
            code_best, code_col = cs, i
        ts = _score(h, COST_PATTERNS)
        if ts > cost_best:
            cost_best, cost_col = ts, i
        ns = _score(h, ARTICLE_NAME_PATTERNS)
        if ns > name_best:
            name_best, name_col = ns, i
        fs = _score(h, FAMILY_PATTERNS)
        if fs > family_best:
            family_best, family_col = fs, i

    if code_col is None:
        print("  WARNING: Could not detect article code column — skipping cost file.")
        return {}

    print(f"  Code column  : [{code_col}] '{headers[code_col]}'")
    if cost_col   is not None: print(f"  Cost column  : [{cost_col}] '{headers[cost_col]}'")
    if name_col   is not None: print(f"  Name column  : [{name_col}] '{headers[name_col]}'")
    if family_col is not None: print(f"  Family column: [{family_col}] '{headers[family_col]}'")

    data_df = df.iloc[header_row_idx + 1:].reset_index(drop=True)
    cost_map: dict[str, dict] = {}
    for _, row in data_df.iterrows():
        vals = row.tolist()
        if code_col >= len(vals):
            continue
        code = _coerce(vals[code_col])
        if not code or code.lower() in ("nan", "none"):
            continue

        entry: dict = {}
        if cost_col is not None and cost_col < len(vals):
            raw = _coerce(vals[cost_col])
            if raw and raw.lower() not in ("nan", "none"):
                entry["cost"] = _clean_money(raw)
        if name_col is not None and name_col < len(vals):
            raw = _coerce(vals[name_col])
            if raw and raw.lower() not in ("nan", "none"):
                entry["name"] = raw
        if family_col is not None and family_col < len(vals):
            raw = _coerce(vals[family_col])
            if raw and raw.lower() not in ("nan", "none"):
                entry["family"] = raw

        cost_map[_norm(code)] = entry

    print(f"  {len(cost_map)} entries loaded.\n")
    return cost_map


# ---------------------------------------------------------------------------
# Main processing
# ---------------------------------------------------------------------------

def process_excel(input_path: str, output_path: str, cost_map: dict | None = None):
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
    raw_headers = source_df.iloc[header_row_idx].tolist()
    headers = [str(h).strip() for h in raw_headers]

    print(f"\n  Header row index: {header_row_idx}")
    print(f"  Raw headers ({len(headers)} columns):")
    for i, h in enumerate(headers):
        print(f"    [{i}] repr={repr(h)}  norm={repr(_norm(h))}")

    mapping = map_columns(headers, verbose=True)
    data_df = source_df.iloc[header_row_idx + 1:].reset_index(drop=True)

    print("  ── Final column mapping ────────────────────────────────────────")
    missing = []
    for k, v in mapping.items():
        if v is not None and v < len(headers):
            print(f"    {k:<30} → col {v}  raw='{headers[v]}'  norm='{_norm(headers[v])}'")
        else:
            optional = k in ("Cliente", "Coste unitario", "Descuento", "Descripción Familia")
            marker = "" if optional else "  ← WARNING: NOT FOUND"
            print(f"    {k:<30} → NOT FOUND{marker}")
            if not optional:
                missing.append(k)
    print()

    output_rows = []
    for _, row in data_df.iterrows():
        row_vals = row.tolist()
        if _is_empty_row(row_vals):
            continue
        rec = build_row(row_vals, mapping)
        # Must have at minimum a document number and an article code
        if not rec["Número de documento"] or not rec["Codigo Articulo"]:
            continue
        # Enrich from the external cost file
        if cost_map:
            entry = cost_map.get(_norm(rec["Codigo Articulo"]))
            if entry:
                if not rec["Coste unitario"] and entry.get("cost"):
                    rec["Coste unitario"] = entry["cost"]
                if not rec["Nombre articulo"] and entry.get("name"):
                    rec["Nombre articulo"] = entry["name"]
                if entry.get("family"):
                    rec["Descripción Familia"] = entry["family"]
                _fill_margin(rec)
        output_rows.append(rec)

    if not output_rows:
        raise ValueError("No data rows could be extracted from the file.")

    print(f"\n  {len(output_rows)} rows extracted. Preview (first 3):")
    for rec in output_rows[:3]:
        print(f"    {rec}")
    print()

    # ---------------------------------------------------------------------------
    # Write output Excel
    # ---------------------------------------------------------------------------
    OUTPUT_COLS = [
        "Número de documento",
        "Codigo Articulo",
        "Nombre articulo",
        "Cliente",
        "Precio",
        "Descuento",
        "Coste unitario",
        "Descripción Familia",
        "Total neto",
        "Margen (%)",
    ]
    COL_WIDTHS = [28, 20, 40, 35, 14, 12, 16, 28, 16, 13]

    wb = Workbook()
    ws = wb.active
    ws.title = "Artículos presupuesto"

    header_font  = Font(name="Arial", bold=True, color="FFFFFF", size=10)
    header_fill  = PatternFill("solid", start_color="2F5496")
    header_align = Alignment(horizontal="center", vertical="center", wrap_text=True)
    cell_font    = Font(name="Arial", size=10)
    alt_fill     = PatternFill("solid", start_color="EEF2F9")
    thin         = Side(style="thin", color="CCCCCC")
    border       = Border(left=thin, right=thin, top=thin, bottom=thin)

    for col_num, (col_name, width) in enumerate(zip(OUTPUT_COLS, COL_WIDTHS), start=1):
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
            # Store numeric columns as real numbers so Excel treats them correctly
            if col_name in ("Precio", "Descuento", "Coste unitario", "Total neto", "Margen (%)") and val:
                try:
                    val = float(val)
                except ValueError:
                    pass
            cell        = ws.cell(row=row_idx, column=col_num, value=val)
            cell.font   = cell_font
            cell.border = border
            if fill:
                cell.fill = fill
            if col_name in ("Precio", "Coste unitario", "Total neto") and isinstance(val, float):
                cell.number_format = "#,##0.0000"
            if col_name == "Descuento" and isinstance(val, float):
                cell.number_format = "0.00\"%\""
            if col_name == "Margen (%)" and isinstance(val, float):
                cell.number_format = "0.00\"%\""

    ws.freeze_panes = "A2"
    wb.save(output_path)
    print(f"Saved → {output_path}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import tkinter as tk
    from tkinter import filedialog, messagebox

    root = tk.Tk()
    root.withdraw()

    input_file = filedialog.askopenfilename(
        title="Select the quote items Excel file to transform",
        filetypes=[("Excel files", "*.xlsx *.xls"), ("All files", "*.*")],
    )

    if not input_file:
        print("No file selected. Exiting.")
        sys.exit(0)

    # Optional: second file with item codes + costs
    cost_file = filedialog.askopenfilename(
        title="Select the cost file (optional — cancel to skip)",
        filetypes=[("Excel files", "*.xlsx *.xls"), ("All files", "*.*")],
    )

    cost_map = {}
    if cost_file:
        print(f"Cost file: {cost_file}")
        cost_map = load_cost_map(cost_file)

    p = Path(input_file)
    output_file = str(p.parent / f"{p.stem}_quoteitems{p.suffix}")

    print(f"Processing: {input_file}")
    try:
        process_excel(input_file, output_file, cost_map=cost_map)
        messagebox.showinfo("Done", f"File saved to:\n{output_file}")
    except Exception as e:
        messagebox.showerror("Error", str(e))
        raise
