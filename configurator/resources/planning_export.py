"""
Export / Import helpers for ProductTemplate production-planning resources.

Export format (one .xlsx sheet):

  Product Template | Stage | Employee Qty | Machine Qty | Prep Time | Work Time | Post Time
  ─────────────────┼───────┼──────────────┼─────────────┼───────────┼───────────┼──────────
  Ventana Corredera│ Corte │ 2            │ 1           │ 5         │ ancho*0.5 │ 3
  Ventana Corredera│ Mecan.│              │             │           │           │
  ...

Import reads the same format, groups by product template name, validates
stage keys, and writes ``production_planning_resources``.
"""

from io import BytesIO

from django.core.exceptions import ValidationError
from django.utils.translation import gettext as _
from openpyxl import Workbook, load_workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter

from configurator.models import ProductTemplate


# ── Column definitions ───────────────────────────────────────────────────────

COLUMNS = [
    ("product_template", _("Product Template")),
    ("product_id", _("Product ID")),
    ("stage", _("Stage")),
    ("employee_qty", _("Employee Qty")),
    ("machine_qty", _("Machine Qty")),
    ("prep_time_min", _("Prep Time")),
    ("work_time_min", _("Work Time")),
    ("post_time_min", _("Post Time")),
]

RESOURCE_FIELDS = ["employee_qty", "machine_qty", "prep_time_min", "work_time_min", "post_time_min"]


# ── Export ────────────────────────────────────────────────────────────────────

def export_planning_xlsx(queryset) -> bytes:
    """
    Generate an Excel workbook from a ProductTemplate queryset.

    Returns raw bytes suitable for an HttpResponse.
    """
    wb = Workbook()
    ws = wb.active
    ws.title = _("Planning Resources")

    # ── Styles ────────────────────────────────────────────────────────────
    header_font = Font(name="Calibri", bold=True, size=11, color="FFFFFF")
    header_fill = PatternFill(start_color="2563EB", end_color="2563EB", fill_type="solid")
    header_alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    thin_border = Border(
        left=Side(style="thin", color="D1D5DB"),
        right=Side(style="thin", color="D1D5DB"),
        top=Side(style="thin", color="D1D5DB"),
        bottom=Side(style="thin", color="D1D5DB"),
    )
    id_fill = PatternFill(start_color="F3F4F6", end_color="F3F4F6", fill_type="solid")
    id_font = Font(name="Calibri", size=10, color="9CA3AF")

    # ── Header row ────────────────────────────────────────────────────────
    for col_idx, (_key, label) in enumerate(COLUMNS, start=1):
        cell = ws.cell(row=1, column=col_idx, value=str(label))
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = header_alignment
        cell.border = thin_border

    # ── Data rows ─────────────────────────────────────────────────────────
    row_num = 2
    for product in queryset.order_by("name"):
        stages = product.production_stages or []
        resources = product.production_planning_resources or {}

        if not stages:
            # Product with no stages: write one empty row so it's visible
            ws.cell(row=row_num, column=1, value=product.name).border = thin_border
            ws.cell(row=row_num, column=2, value=product.pk).border = thin_border
            ws.cell(row=row_num, column=3, value="").border = thin_border
            for ci in range(4, len(COLUMNS) + 1):
                ws.cell(row=row_num, column=ci).border = thin_border
            row_num += 1
            continue

        for stage_key in stages:
            stage_data = resources.get(stage_key, {})

            ws.cell(row=row_num, column=1, value=product.name).border = thin_border
            id_cell = ws.cell(row=row_num, column=2, value=product.pk)
            id_cell.border = thin_border
            id_cell.fill = id_fill
            id_cell.font = id_font
            ws.cell(row=row_num, column=3, value=stage_key).border = thin_border

            for field_idx, field_name in enumerate(RESOURCE_FIELDS):
                value = stage_data.get(field_name, "")
                cell = ws.cell(row=row_num, column=4 + field_idx, value=value if value != 0 else value)
                cell.border = thin_border
                cell.alignment = Alignment(horizontal="center")

            row_num += 1

    # ── Column widths ─────────────────────────────────────────────────────
    col_widths = [30, 12, 20, 15, 15, 15, 15, 15]
    for i, w in enumerate(col_widths, start=1):
        ws.column_dimensions[get_column_letter(i)].width = w

    # Freeze header
    ws.freeze_panes = "A2"

    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()


# ── Import ────────────────────────────────────────────────────────────────────

def import_planning_xlsx(file_bytes, *, company=None) -> dict:
    """
    Parse an uploaded Excel file and apply planning resources to each
    ProductTemplate.

    Returns a summary dict:
        {"updated": [list of product names], "errors": [list of error strings]}
    """
    wb = load_workbook(filename=BytesIO(file_bytes), read_only=True, data_only=False)
    ws = wb.active

    rows = list(ws.iter_rows(min_row=2, values_only=True))

    # Group rows by product_id
    product_stages: dict[int, dict] = {}  # {product_id: {stage_key: {field: val}}}
    errors = []

    for row_idx, row in enumerate(rows, start=2):
        if not row or len(row) < 3:
            continue

        product_name = str(row[0] or "").strip()
        try:
            product_id = int(row[1]) if row[1] else None
        except (ValueError, TypeError):
            product_id = None

        stage_key = str(row[2] or "").strip()

        if not product_id:
            if product_name:
                errors.append(
                    _("Row %(row)d: Missing Product ID for '%(name)s'.")
                    % {"row": row_idx, "name": product_name}
                )
            continue

        if not stage_key:
            continue  # Skip rows with no stage (product without stages)

        stage_data = {}
        for field_idx, field_name in enumerate(RESOURCE_FIELDS):
            col_value = row[3 + field_idx] if len(row) > 3 + field_idx else None

            if col_value is None or col_value == "":
                stage_data[field_name] = 0 if field_name in ("employee_qty", "machine_qty") else "0"
            elif field_name in ("employee_qty", "machine_qty"):
                try:
                    stage_data[field_name] = int(col_value)
                except (ValueError, TypeError):
                    errors.append(
                        _("Row %(row)d, %(field)s: Expected integer, got '%(val)s'.")
                        % {"row": row_idx, "field": field_name, "val": col_value}
                    )
                    stage_data[field_name] = 0
            else:
                # Time fields can be expressions (strings) or numbers
                stage_data[field_name] = str(col_value).strip() if col_value else "0"

        product_stages.setdefault(product_id, {})[stage_key] = stage_data

    # Apply to database
    updated = []

    for product_id, stages_dict in product_stages.items():
        try:
            qs = ProductTemplate.objects.filter(pk=product_id)
            if company:
                qs = qs.filter(company=company)
            product = qs.get()
        except ProductTemplate.DoesNotExist:
            errors.append(
                _("Product ID %(id)d not found (or belongs to a different company).")
                % {"id": product_id}
            )
            continue

        # Validate stage keys
        valid_stages = set(product.production_stages or [])
        invalid_stages = set(stages_dict.keys()) - valid_stages
        if invalid_stages:
            errors.append(
                _("Product '%(name)s' (ID %(id)d): Invalid stages: %(stages)s.")
                % {
                    "name": product.name,
                    "id": product_id,
                    "stages": ", ".join(sorted(invalid_stages)),
                }
            )
            continue

        product.production_planning_resources = stages_dict
        product.production_planning_configured = True
        try:
            product.full_clean()
            product.save(update_fields=["production_planning_resources", "production_planning_configured"])
            updated.append(product.name)
        except ValidationError as e:
            errors.append(
                _("Product '%(name)s' (ID %(id)d): %(error)s")
                % {"name": product.name, "id": product_id, "error": str(e)}
            )

    return {"updated": updated, "errors": errors}
