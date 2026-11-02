"""
Service for importing sales invoices from an Excel file.

Expected Excel columns:
    invoice_number  | date_issued     | due_date       | description
    company         | client_cif      | client_name    | base_amount
    iva_percentage  | iva_amount      | retention      | total

Client is matched by CIF (tax_identifier) within the row's company. If not found, a new
Client is auto-created with the client_name value.
Company is matched by name (case-insensitive).
Duplicates (same company + invoice_number) are silently skipped.

All imported invoices are marked as:
    status       = FINAL
    is_external  = True
    payment_status = UNPAID
"""

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import BinaryIO

import openpyxl
from django.contrib.contenttypes.models import ContentType
from django.db import IntegrityError, transaction

from company.models import Company
from core.services.client_service import get_or_create_client
from invoices.models import (
    AppliedTax,
    InvoicePaymentStatus,
    SalesInvoice,
    SalesInvoiceItem,
    SalesInvoiceStatus,
    TaxType,
)
from sales.models import Client


# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------

@dataclass
class ImportRowResult:
    row_number: int
    invoice_number: str = ""
    client_cif: str = ""
    status: str = ""  # "created", "skipped", "error"
    message: str = ""


@dataclass
class ImportResult:
    total_rows: int = 0
    created: int = 0
    skipped: int = 0
    errors: int = 0
    warnings: int = 0
    rows: list[ImportRowResult] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_decimal(value) -> Decimal | None:
    """Parse a value that might be a string with comma decimals or a number."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return Decimal(str(value))
    if isinstance(value, str):
        cleaned = value.strip().replace(",", ".")
        if not cleaned:
            return None
        try:
            return Decimal(cleaned)
        except InvalidOperation:
            return None
    return None


def _parse_date(value, default_year: int | None = None):
    """
    Parse a date value. Supports:
    - datetime objects (from openpyxl)
    - DD/MM/YYYY strings
    - DD/MM strings (uses default_year or current year)
    """
    if isinstance(value, datetime):
        return value.date()
    if hasattr(value, 'date') and callable(value.date):
        return value.date()
    if not isinstance(value, str):
        return None

    value = value.strip()
    if not value:
        return None

    for fmt in ("%d/%m/%Y", "%d/%m/%y"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue

    # Try DD/MM (no year)
    try:
        parsed = datetime.strptime(value, "%d/%m")
        year = default_year or datetime.now().year
        return parsed.replace(year=year).date()
    except ValueError:
        pass

    return None


def _clean_cif(value) -> str:
    """Normalize a CIF/NIF value for matching."""
    if value is None:
        return ""
    return str(value).strip().upper()


def _find_company(name: str) -> Company | None:
    """
    Find a company by name (case-insensitive).
    Also tries matching after removing spaces so 'SANCARLOS' matches 'San Carlos'.
    """
    if not name:
        return None
    clean = name.strip()
    # Exact match first
    company = Company.objects.filter(name__iexact=clean).first()
    if company:
        return company
    # Try matching after removing all spaces
    for c in Company.objects.all():
        if c.name.replace(' ', '').lower() == clean.replace(' ', '').lower():
            return c
    return None


def _get_iva_tax_type() -> TaxType | None:
    """Find the IVA tax type."""
    return TaxType.objects.filter(
        tax_name__iexact="IVA",
        country="ES",
        behavior="add",
        is_active=True,
    ).first()


def _get_irpf_tax_type() -> TaxType | None:
    """Find the IRPF tax type."""
    return TaxType.objects.filter(
        tax_name__iexact="IRPF",
        country="ES",
        behavior="subtract",
        is_active=True,
    ).first()


def _read_header_mapping(ws) -> dict[str, int]:
    """
    Read the first row and return a mapping of column_name -> column_index.
    Column names are lowercased and stripped.
    Also provides backward compatibility: provider_cif -> client_cif, provider_name -> client_name.
    """
    mapping = {}
    for idx, cell in enumerate(ws[1]):
        if cell.value:
            col_name = str(cell.value).strip().lower()
            mapping[col_name] = idx

    # Backward compatibility: alias old column names to new ones
    if "client_cif" not in mapping and "provider_cif" in mapping:
        mapping["client_cif"] = mapping["provider_cif"]
    if "client_name" not in mapping and "provider_name" in mapping:
        mapping["client_name"] = mapping["provider_name"]

    return mapping


def _get_cell(row, col_map, col_name):
    """Get the value of a cell by column name."""
    idx = col_map.get(col_name)
    if idx is not None and idx < len(row):
        return row[idx].value
    return None


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def import_sales_invoices_from_excel(
    file_obj: BinaryIO,
    default_year: int | None = None,
    sheet_name: str | None = None,
    company: Company | None = None,
) -> ImportResult:
    """
    Import sales invoices from an Excel file.

    With ``company``, every row is imported into it and the company column is
    ignored: what a user who may only see that company imports with.

    Automatically handles multi-IVA invoices: when the same invoice_number +
    client_cif appears on multiple rows (each with a different IVA rate),
    they are grouped into a single SalesInvoice with multiple AppliedTax entries.

    Expected columns (first row = headers):
        invoice_number, date_issued, due_date, description,
        company, client_cif, client_name, base_amount,
        iva_percentage, iva_amount, retention, total

    Also accepts provider_cif/provider_name as aliases for backward compat.
    """
    result = ImportResult()

    wb = openpyxl.load_workbook(file_obj, read_only=True, data_only=True)

    # Select the appropriate sheet
    if sheet_name:
        if sheet_name not in wb.sheetnames:
            result.errors = 1
            result.rows.append(ImportRowResult(
                row_number=0,
                status="error",
                message=f"Sheet '{sheet_name}' not found. "
                        f"Available sheets: {', '.join(wb.sheetnames)}",
            ))
            wb.close()
            return result
        ws = wb[sheet_name]
    else:
        ws = wb.active

    # Read header mapping
    col_map = _read_header_mapping(ws)

    # Validate required columns
    missing = []
    for required_col in ["invoice_number", "date_issued", "client_cif", "base_amount"]:
        if required_col not in col_map:
            missing.append(required_col)

    if missing:
        result.errors = 1
        result.rows.append(ImportRowResult(
            row_number=0,
            status="error",
            message=f"Missing required columns: {', '.join(missing)}. "
                    f"Found columns: {', '.join(col_map.keys())}",
        ))
        wb.close()
        return result

    # ---- Phase 1: Read all rows and group by (invoice_number, client_cif) ----
    from collections import OrderedDict

    @dataclass
    class _IvaLine:
        base_amount: Decimal
        iva_percentage: Decimal
        iva_amount: Decimal | None
        retention: Decimal
        total: Decimal | None

    @dataclass
    class _InvoiceGroup:
        invoice_number: str
        company_name: str
        cif: str
        client_name: str
        date_issued: object  # date or None
        due_date: object  # date or None
        description: str
        first_row: int
        iva_lines: list = field(default_factory=list)

    groups: OrderedDict[tuple[str, str], _InvoiceGroup] = OrderedDict()

    for row in ws.iter_rows(min_row=2, max_row=ws.max_row):
        result.total_rows += 1
        if row[0].value is None:
            continue
        row_num = row[0].row

        invoice_number = str(_get_cell(row, col_map, "invoice_number") or "").strip()
        if not invoice_number:
            result.skipped += 1
            result.rows.append(ImportRowResult(
                row_number=row_num,
                status="skipped",
                message="Empty invoice number",
            ))
            continue

        cif = _clean_cif(_get_cell(row, col_map, "client_cif"))
        group_key = (invoice_number, cif)

        base_amount = _parse_decimal(_get_cell(row, col_map, "base_amount")) or Decimal("0.00")
        iva_pct = _parse_decimal(_get_cell(row, col_map, "iva_percentage")) or Decimal("0")
        iva_amount = _parse_decimal(_get_cell(row, col_map, "iva_amount"))
        retention = _parse_decimal(_get_cell(row, col_map, "retention")) or Decimal("0")
        total = _parse_decimal(_get_cell(row, col_map, "total"))

        iva_line = _IvaLine(
            base_amount=base_amount,
            iva_percentage=iva_pct,
            iva_amount=iva_amount,
            retention=retention,
            total=total,
        )

        if group_key not in groups:
            company_name = str(_get_cell(row, col_map, "company") or "").strip()
            client_name_val = str(_get_cell(row, col_map, "client_name") or "").strip()
            date_issued = _parse_date(
                _get_cell(row, col_map, "date_issued"), default_year=default_year
            )
            due_date = _parse_date(
                _get_cell(row, col_map, "due_date"), default_year=default_year
            )
            description = str(_get_cell(row, col_map, "description") or "").strip()

            groups[group_key] = _InvoiceGroup(
                invoice_number=invoice_number,
                company_name=company_name,
                cif=cif,
                client_name=client_name_val,
                date_issued=date_issued,
                due_date=due_date,
                description=description,
                first_row=row_num,
                iva_lines=[iva_line],
            )
        else:
            groups[group_key].iva_lines.append(iva_line)

    wb.close()

    # ---- Phase 2: Create invoices from groups ----
    iva_tax_type = _get_iva_tax_type()
    irpf_tax_type = _get_irpf_tax_type()
    forced_company = company
    company_cache: dict[str, Company | None] = {}
    client_cache: dict[tuple[str, int], Client | None] = {}

    for group_key, group in groups.items():
        row_num = group.first_row
        invoice_number = group.invoice_number
        cif = group.cif
        num_iva_lines = len(group.iva_lines)

        # --- Company ---
        if group.company_name not in company_cache:
            company_cache[group.company_name] = forced_company or _find_company(group.company_name)
        company = company_cache[group.company_name]

        if not company:
            result.errors += 1
            result.rows.append(ImportRowResult(
                row_number=row_num,
                invoice_number=invoice_number,
                status="error",
                message=f"Company '{group.company_name}' not found",
            ))
            continue

        # --- Client ---
        cache_key = (cif, company.pk)
        if cache_key not in client_cache:
            client_cache[cache_key] = get_or_create_client(
                cif, group.client_name, company
            )
        client = client_cache[cache_key]

        if not client:
            result.errors += 1
            result.rows.append(ImportRowResult(
                row_number=row_num,
                invoice_number=invoice_number,
                client_cif=cif,
                status="error",
                message="Empty CIF, cannot match client",
            ))
            continue

        # --- Aggregate amounts across all IVA lines ---
        total_base = sum(line.base_amount for line in group.iva_lines)
        total_iva = sum(
            line.iva_amount if line.iva_amount is not None
            else (line.base_amount * line.iva_percentage / Decimal("100")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            for line in group.iva_lines
        )
        total_retention = sum(line.retention for line in group.iva_lines)
        total_amount = sum(
            line.total if line.total is not None else line.base_amount
            for line in group.iva_lines
        )

        tax_total = total_iva - total_retention

        # --- Create SalesInvoice ---
        try:
            with transaction.atomic():
                # Check for duplicate
                if SalesInvoice.objects.filter(
                    company=company,
                    invoice_number=invoice_number,
                ).exists():
                    result.skipped += 1
                    result.rows.append(ImportRowResult(
                        row_number=row_num,
                        invoice_number=invoice_number,
                        client_cif=cif,
                        status="skipped",
                        message=f"Duplicate (already exists)"
                               + (f" — {num_iva_lines} IVA lines" if num_iva_lines > 1 else ""),
                    ))
                    continue

                # Create as PROFORMA first (AppliedTax.save() blocks new taxes on FINAL)
                sales_invoice = SalesInvoice(
                    company=company,
                    status=SalesInvoiceStatus.PROFORMA,
                    is_external=True,
                    invoice_number=invoice_number,
                    date_issued=group.date_issued,
                    due_date=group.due_date,
                    client=client,
                    subtotal_amount=total_base,
                    tax_amount=tax_total,
                    total_amount=total_amount,
                    payment_status=InvoicePaymentStatus.UNPAID,
                    notes="Imported from Excel",
                )
                sales_invoice.save()

                invoice_ct = ContentType.objects.get_for_model(SalesInvoice)

                # Create one line item + tax per IVA line
                for order, line in enumerate(group.iva_lines):
                    # Line item
                    if line.base_amount:
                        desc = group.description or "Imported line item"
                        if num_iva_lines > 1:
                            iva_label = str(line.iva_percentage).rstrip('0').rstrip('.')
                            desc = f"{desc} (IVA {iva_label}%)"

                        SalesInvoiceItem.objects.create(
                            invoice=sales_invoice,
                            description=desc,
                            quantity=Decimal("1"),
                            unit_price=line.base_amount,
                        )

                    # IVA tax
                    if iva_tax_type and line.iva_percentage > 0:
                        computed_iva = line.iva_amount
                        if computed_iva is None:
                            computed_iva = (
                                line.base_amount * line.iva_percentage / Decimal("100")
                            ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

                        AppliedTax.objects.create(
                            tax_type=iva_tax_type,
                            selected_percentage=line.iva_percentage,
                            base_amount=line.base_amount,
                            calculated_amount=computed_iva,
                            content_type=invoice_ct,
                            object_id=sales_invoice.pk,
                            order=order,
                        )

                # Apply IRPF retention (aggregate across all lines)
                if irpf_tax_type and total_retention and total_retention > 0:
                    irpf_pct = Decimal("0")
                    if total_base > 0:
                        irpf_pct = (
                            total_retention / total_base * Decimal("100")
                        ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

                    AppliedTax.objects.create(
                        tax_type=irpf_tax_type,
                        selected_percentage=irpf_pct,
                        base_amount=total_base,
                        calculated_amount=total_retention,
                        content_type=invoice_ct,
                        object_id=sales_invoice.pk,
                        order=len(group.iva_lines),
                    )

                # Recalculate totals while still PROFORMA
                sales_invoice.recalculate_totals(save=True)

                # Finalize with the original amounts from the Excel
                sales_invoice.status = SalesInvoiceStatus.FINAL
                sales_invoice.subtotal_amount = total_base
                sales_invoice.tax_amount = tax_total
                sales_invoice.total_amount = total_amount
                sales_invoice.save(update_fields=[
                    'status', 'subtotal_amount', 'tax_amount', 'total_amount'
                ])

                msg = "OK"
                if num_iva_lines > 1:
                    msg = f"OK — {num_iva_lines} IVA lines"

                calculated_total = total_base + tax_total
                if abs(calculated_total - total_amount) > Decimal("0.01"):
                    result.warnings += 1
                    msg += (
                        f" ⚠ Total mismatch: calculated {calculated_total} "
                        f"vs Excel 'total' column {total_amount}"
                    )

                result.created += 1
                result.rows.append(ImportRowResult(
                    row_number=row_num,
                    invoice_number=invoice_number,
                    client_cif=cif,
                    status="created",
                    message=msg,
                ))

        except IntegrityError as e:
            result.skipped += 1
            result.rows.append(ImportRowResult(
                row_number=row_num,
                invoice_number=invoice_number,
                client_cif=cif,
                status="skipped",
                message=f"Duplicate: {e}",
            ))
        except Exception as e:
            result.errors += 1
            result.rows.append(ImportRowResult(
                row_number=row_num,
                invoice_number=invoice_number,
                client_cif=cif,
                status="error",
                message=str(e),
            ))

    return result