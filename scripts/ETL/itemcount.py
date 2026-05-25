#!/usr/bin/env python3
# pip install openpyxl pandas
# python ETL/itemscount.py

"""
itemscount.py
-------------
Reads any Excel file, finds the 'Cod-Artículo' column, and outputs a clean
Excel showing how many times each code appears.

Usage:
    python itemscount.py
    (a Finder dialog will open to select the input file)
"""

import sys
import re
from pathlib import Path

import pandas as pd
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter

import tkinter as tk
from tkinter import filedialog, messagebox


# ---------------------------------------------------------------------------
# Column detection
# ---------------------------------------------------------------------------

COD_PATTERNS = [
    r"cod[\.\-_\s]*art",
    r"c[oó]digo[\s_\-]*art",
    r"art[\.\-_\s]*cod",
    r"^cod$",
    r"^c[oó]digo$",
    r"^art[íi]culo$",
    r"ref[\.\-_\s]*art",
]


def find_header_and_col(df_raw: pd.DataFrame):
    """Return (header_row_idx, col_idx) for Cod-Artículo."""
    for row_idx, row in df_raw.iterrows():
        for col_idx, cell in enumerate(row.astype(str)):
            for pat in COD_PATTERNS:
                if re.search(pat, str(cell).strip(), re.IGNORECASE):
                    return row_idx, col_idx
    return None, None


# ---------------------------------------------------------------------------
# Main processing
# ---------------------------------------------------------------------------

def process_excel(input_path: str, output_path: str):
    all_sheets = pd.read_excel(input_path, sheet_name=None, header=None, dtype=str)

    source_df = None
    for sheet_name, df in all_sheets.items():
        if not df.dropna(how="all").empty:
            source_df = df.dropna(how="all").reset_index(drop=True)
            print(f"  Using sheet: '{sheet_name}'")
            break

    if source_df is None:
        raise ValueError("No data found in the file.")

    header_row, col_idx = find_header_and_col(source_df)

    if col_idx is None:
        raise ValueError(
            "Could not find a 'Cod-Artículo' column. "
            "Check that the column header exists in the file."
        )

    print(f"  Found header at row {header_row}, column {col_idx} "
          f"('{source_df.iloc[header_row, col_idx]}')")

    data = source_df.iloc[header_row + 1:, col_idx].reset_index(drop=True)
    data = data[~data.isin(["", "nan", "None", "NaN"])].dropna()

    if data.empty:
        raise ValueError("No values found under the Cod-Artículo column.")

    counts = (
        data.value_counts()
        .reset_index()
        .rename(columns={"index": "Cod-Artículo", data.name: "Veces"})
    )
    counts.columns = ["Cod-Artículo", "Veces"]
    counts = counts.sort_values("Veces", ascending=False).reset_index(drop=True)

    duplicates = counts[counts["Veces"] > 1]

    print(f"  Total unique codes  : {len(counts)}")
    print(f"  Codes appearing >1x : {len(duplicates)}")
    print(f"  Codes appearing once: {(counts['Veces'] == 1).sum()}")

    # ---------------------------------------------------------------------------
    # Build output Excel
    # ---------------------------------------------------------------------------
    wb = Workbook()

    ws1 = wb.active
    ws1.title = "Conteo Completo"
    _write_sheet(ws1, counts, f"Total códigos únicos: {len(counts)}")

    ws2 = wb.create_sheet("Solo Repetidos")
    _write_sheet(ws2, duplicates.reset_index(drop=True),
                 f"Códigos repetidos (aparecen más de una vez): {len(duplicates)}")

    wb.save(output_path)
    print(f"\n✓ Wrote results → {output_path}")


def _write_sheet(ws, df: pd.DataFrame, subtitle: str):
    thin = Side(style="thin", color="CCCCCC")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)

    ws.merge_cells("A1:B1")
    sub_cell = ws["A1"]
    sub_cell.value = subtitle
    sub_cell.font = Font(name="Arial", italic=True, size=9, color="555555")
    sub_cell.alignment = Alignment(horizontal="left", vertical="center")
    ws.row_dimensions[1].height = 18

    header_font  = Font(name="Arial", bold=True, color="FFFFFF", size=10)
    header_fill  = PatternFill("solid", start_color="2F5496")
    header_align = Alignment(horizontal="center", vertical="center")

    for col_num, col_name in enumerate(["Cod-Artículo", "Veces"], start=1):
        cell = ws.cell(row=2, column=col_num, value=col_name)
        cell.font      = header_font
        cell.fill      = header_fill
        cell.alignment = header_align
        cell.border    = border

    ws.row_dimensions[2].height = 22
    ws.column_dimensions["A"].width = 22
    ws.column_dimensions["B"].width = 12

    alt_fill  = PatternFill("solid", start_color="EEF2F9")
    cell_font = Font(name="Arial", size=10)

    for row_idx, (_, row) in enumerate(df.iterrows(), start=3):
        fill = alt_fill if row_idx % 2 == 0 else None
        for col_num, val in enumerate([row["Cod-Artículo"], row["Veces"]], start=1):
            cell            = ws.cell(row=row_idx, column=col_num, value=val)
            cell.font       = cell_font
            cell.border     = border
            if fill:
                cell.fill   = fill
            if col_num == 2:
                cell.alignment = Alignment(horizontal="center")

    ws.freeze_panes = "A3"


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    root = tk.Tk()
    root.withdraw()

    input_file = filedialog.askopenfilename(
        title="Select the Excel file to analyse",
        filetypes=[("Excel files", "*.xlsx *.xls *.xlsm"), ("All files", "*.*")],
    )

    if not input_file:
        print("No file selected. Exiting.")
        sys.exit(0)

    p = Path(input_file)
    output_file = str(p.parent / f"{p.stem}_cod_counts.xlsx")

    print(f"Processing: {input_file}")
    try:
        process_excel(input_file, output_file)
        messagebox.showinfo("Done", f"File saved to:\n{output_file}")
    except Exception as e:
        messagebox.showerror("Error", str(e))
        raise