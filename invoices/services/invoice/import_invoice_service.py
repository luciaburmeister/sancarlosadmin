"""
Service for importing invoices from a generic Excel file.

Expected Excel columns:
    invoice_number  | date_issued (DD/MM/YYYY) | provider_cif | provider_name
    description     | base_amount              | iva_percentage | iva_amount
    retention       | total                    | company

Provider is matched by CIF (tax_identifier). If not found, a new
Provider is auto-created with the provider_name value.
Company is matched by name (case-insensitive).
Duplicates (same provider + invoice_number) are silently skipped.
"""

from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import BinaryIO

import openpyxl
from django.contrib.contenttypes.models import ContentType
from django.db import IntegrityError, transaction

from company.models import Company
from invoices.models import (
    AppliedTax,
    Invoice,
    InvoiceItem,
    TaxType,
)
from materials.models import Provider


EXPECTED_COLUMNS = [
    "invoice_number",
    "date_issued",
    "provider_cif",
    "provider_name",
    "description",
    "base_amount",
    "iva_percentage",
    "iva_amount",
    "retention",
    "total",
    "company",
]


@dataclass
class ImportRowResult:
    row_number: int
    invoice_number: str = ""
    provider_cif: str = ""
    status: str = ""  # "created", "skipped", "error"
    message: str = ""


@dataclass
class ImportResult:
    total_rows: int = 0
    created: int = 0
    skipped: int = 0
    errors: int = 0
    rows: list[ImportRowResult] = field(default_factory=list)


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


def _parse_date(value, default_year: int | None = None) -> datetime | None:
    """
    Parse a date value. Supports:
    - datetime objects (from openpyxl)
    - DD/MM/YYYY strings
    - DD/MM strings (uses default_year or current year)
    """
    if isinstance(value, datetime):
        return value.date()
    if not isinstance(value, str):
        return None

    value = value.strip()
    if not value:
        return None

    # Try DD/MM/YYYY first
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


def _find_or_create_provider(cif: str, name: str, company: Company) -> Provider | None:
    """
    Find a provider by CIF (tax_identifier). If not found, create one.
    Returns None only if CIF is empty.
    """
    if not cif:
        return None

    # Try to find existing provider by CIF (case-insensitive)
    provider = Provider.objects.filter(
        tax_identifier__iexact=cif,
        company=company,
    ).first()

    if provider:
        return provider

    # Also try without company filter (cross-company match)
    provider = Provider.objects.filter(
        tax_identifier__iexact=cif,
    ).first()

    if provider:
        return provider

    # Auto-create provider
    provider_name = name.strip() if name else f"Provider {cif}"
    provider = Provider.objects.create(
        name=provider_name,
        tax_identifier=cif,
        company=company,
    )
    return provider


def _find_company(name: str) -> Company | None:
    """Find a company by name (case-insensitive)."""
    if not name:
        return None
    return Company.objects.filter(name__iexact=name.strip()).first()


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
    """
    mapping = {}
    for idx, cell in enumerate(ws[1]):
        if cell.value:
            col_name = str(cell.value).strip().lower()
            mapping[col_name] = idx
    return mapping


def import_invoices_from_excel(
    file_obj: BinaryIO,
    default_year: int | None = None,
    sheet_name: str | None = None,
    multi_iva_mode: bool = False,
) -> ImportResult:
    """
    Import invoices from an Excel file with the generic format.

    Args:
        file_obj: The uploaded Excel file.
        default_year: Year to use for dates without year (e.g., DD/MM format).
        sheet_name: Name of the Excel sheet to import. If None, uses the active sheet.
        multi_iva_mode: If True, groups rows by invoice_number + provider_cif
            and creates one invoice with multiple IVA tax lines per group.
            Use this for sheets like '2 TIPO IVA EXP' and '3 TIPO IVA EXP'.

    Returns:
        ImportResult with summary and per-row details.
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
    for required_col in ["invoice_number", "date_issued", "provider_cif", "base_amount"]:
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

    if multi_iva_mode:
        _import_multi_iva(ws, col_map, result, default_year)
        wb.close()
        return result

    _import_single_iva(ws, col_map, result, default_year)
    wb.close()
    return result


def _get_cell(row, col_map, col_name):
    """Get the value of a cell by column name."""
    idx = col_map.get(col_name)
    if idx is not None and idx < len(row):
        return row[idx].value
    return None


def _import_single_iva(ws, col_map: dict, result: ImportResult, default_year: int | None):
    """
    Import invoices where each row is a single invoice with one IVA rate.
    This is the original import logic.
    """
    # Cache tax types
    iva_tax_type = _get_iva_tax_type()
    irpf_tax_type = _get_irpf_tax_type()

    # Cache companies and providers for efficiency
    company_cache: dict[str, Company | None] = {}
    provider_cache: dict[tuple[str, int], Provider | None] = {}

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

        # --- Company ---
        company_name = str(_get_cell(row, col_map, "company") or "").strip()
        if company_name not in company_cache:
            company_cache[company_name] = _find_company(company_name)
        company = company_cache[company_name]

        if not company:
            result.errors += 1
            result.rows.append(ImportRowResult(
                row_number=row_num,
                invoice_number=invoice_number,
                status="error",
                message=f"Company '{company_name}' not found",
            ))
            continue

        # --- Provider ---
        cif = _clean_cif(_get_cell(row, col_map, "provider_cif"))
        provider_name = str(_get_cell(row, col_map, "provider_name") or "").strip()

        cache_key = (cif, company.pk)
        if cache_key not in provider_cache:
            provider_cache[cache_key] = _find_or_create_provider(cif, provider_name, company)
        provider = provider_cache[cache_key]

        if not provider:
            result.errors += 1
            result.rows.append(ImportRowResult(
                row_number=row_num,
                invoice_number=invoice_number,
                provider_cif=cif,
                status="error",
                message="Empty CIF, cannot match provider",
            ))
            continue

        # --- Date ---
        date_issued = _parse_date(_get_cell(row, col_map, "date_issued"), default_year=default_year)

        # --- Amounts ---
        base_amount = _parse_decimal(_get_cell(row, col_map, "base_amount")) or Decimal("0.00")
        iva_pct = _parse_decimal(_get_cell(row, col_map, "iva_percentage")) or Decimal("0")
        iva_amount = _parse_decimal(_get_cell(row, col_map, "iva_amount"))
        retention = _parse_decimal(_get_cell(row, col_map, "retention")) or Decimal("0")
        total = _parse_decimal(_get_cell(row, col_map, "total"))
        description = str(_get_cell(row, col_map, "description") or "").strip()

        # --- Create Invoice ---
        try:
            with transaction.atomic():
                # Check for duplicate
                if Invoice.objects.filter(
                    provider=provider,
                    invoice_number=invoice_number,
                ).exists():
                    result.skipped += 1
                    result.rows.append(ImportRowResult(
                        row_number=row_num,
                        invoice_number=invoice_number,
                        provider_cif=cif,
                        status="skipped",
                        message="Duplicate (already exists)",
                    ))
                    continue

                invoice = Invoice(
                    invoice_number=invoice_number,
                    date_issued=date_issued,
                    provider=provider,
                    company=company,
                    gross_amount=base_amount,
                    total_amount=total or base_amount,
                    image_hash="",  # No file uploaded for imports
                    needs_manual_review=True,
                    ocr_processed=True,  # Mark as processed since data comes from Excel
                )
                invoice.save()

                # Create a single line item for the base amount
                if base_amount:
                    InvoiceItem.objects.create(
                        invoice=invoice,
                        description=description or f"Imported line item",
                        quantity=Decimal("1"),
                        unit_price=base_amount,
                    )

                # Apply IVA tax
                invoice_ct = ContentType.objects.get_for_model(Invoice)

                if iva_tax_type and iva_pct > 0:
                    # Calculate IVA amount if not provided
                    if iva_amount is None:
                        iva_amount = (base_amount * iva_pct / Decimal("100")).quantize(Decimal("0.01"))

                    AppliedTax.objects.create(
                        tax_type=iva_tax_type,
                        selected_percentage=iva_pct,
                        base_amount=base_amount,
                        calculated_amount=iva_amount,
                        content_type=invoice_ct,
                        object_id=invoice.pk,
                        order=0,
                    )

                # Apply IRPF retention
                if irpf_tax_type and retention and retention > 0:
                    # Calculate IRPF percentage from amounts
                    irpf_pct = Decimal("0")
                    if base_amount > 0:
                        irpf_pct = (retention / base_amount * Decimal("100")).quantize(Decimal("0.01"))

                    AppliedTax.objects.create(
                        tax_type=irpf_tax_type,
                        selected_percentage=irpf_pct,
                        base_amount=base_amount,
                        calculated_amount=retention,
                        content_type=invoice_ct,
                        object_id=invoice.pk,
                        order=1,
                    )

                # Recalculate totals
                invoice.recalculate_totals(save=True)

                result.created += 1
                result.rows.append(ImportRowResult(
                    row_number=row_num,
                    invoice_number=invoice_number,
                    provider_cif=cif,
                    status="created",
                    message="OK",
                ))

        except IntegrityError as e:
            result.skipped += 1
            result.rows.append(ImportRowResult(
                row_number=row_num,
                invoice_number=invoice_number,
                provider_cif=cif,
                status="skipped",
                message=f"Duplicate: {e}",
            ))
        except Exception as e:
            result.errors += 1
            result.rows.append(ImportRowResult(
                row_number=row_num,
                invoice_number=invoice_number,
                provider_cif=cif,
                status="error",
                message=str(e),
            ))


@dataclass
class _IvaLine:
    """A single IVA tax line from a multi-IVA row."""
    base_amount: Decimal
    iva_percentage: Decimal
    iva_amount: Decimal | None
    retention: Decimal
    total: Decimal | None


@dataclass
class _InvoiceGroup:
    """All data for a grouped invoice from multi-IVA sheets."""
    invoice_number: str
    company_name: str
    cif: str
    provider_name: str
    date_issued: object  # date or None
    description: str
    first_row: int
    iva_lines: list[_IvaLine] = field(default_factory=list)


def _import_multi_iva(ws, col_map: dict, result: ImportResult, default_year: int | None):
    """
    Import invoices from a multi-IVA sheet where the same invoice appears
    on multiple rows, each with a different IVA rate.

    Rows are grouped by (invoice_number, provider_cif). One Invoice is created
    per group, with multiple AppliedTax entries (one per IVA rate) and
    multiple InvoiceItem entries (one per IVA line base amount).
    """
    # Cache tax types
    iva_tax_type = _get_iva_tax_type()
    irpf_tax_type = _get_irpf_tax_type()

    # Cache companies and providers
    company_cache: dict[str, Company | None] = {}
    provider_cache: dict[tuple[str, int], Provider | None] = {}

    # ---- Phase 1: Read all rows and group by (invoice_number, cif) ----
    groups: OrderedDict[tuple[str, str], _InvoiceGroup] = OrderedDict()

    for row in ws.iter_rows(min_row=2, max_row=ws.max_row):
        result.total_rows += 1
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

        cif = _clean_cif(_get_cell(row, col_map, "provider_cif"))
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
            provider_name = str(_get_cell(row, col_map, "provider_name") or "").strip()
            date_issued = _parse_date(
                _get_cell(row, col_map, "date_issued"), default_year=default_year
            )
            description = str(_get_cell(row, col_map, "description") or "").strip()

            groups[group_key] = _InvoiceGroup(
                invoice_number=invoice_number,
                company_name=company_name,
                cif=cif,
                provider_name=provider_name,
                date_issued=date_issued,
                description=description,
                first_row=row_num,
                iva_lines=[iva_line],
            )
        else:
            groups[group_key].iva_lines.append(iva_line)

    # ---- Phase 2: Create invoices from groups ----
    for group_key, group in groups.items():
        row_num = group.first_row
        invoice_number = group.invoice_number
        cif = group.cif
        num_iva_lines = len(group.iva_lines)

        # --- Company ---
        if group.company_name not in company_cache:
            company_cache[group.company_name] = _find_company(group.company_name)
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

        # --- Provider ---
        cache_key = (cif, company.pk)
        if cache_key not in provider_cache:
            provider_cache[cache_key] = _find_or_create_provider(
                cif, group.provider_name, company
            )
        provider = provider_cache[cache_key]

        if not provider:
            result.errors += 1
            result.rows.append(ImportRowResult(
                row_number=row_num,
                invoice_number=invoice_number,
                provider_cif=cif,
                status="error",
                message="Empty CIF, cannot match provider",
            ))
            continue

        # --- Aggregate amounts ---
        total_base = sum(line.base_amount for line in group.iva_lines)
        total_iva = sum(
            line.iva_amount if line.iva_amount is not None
            else (line.base_amount * line.iva_percentage / Decimal("100")).quantize(Decimal("0.01"))
            for line in group.iva_lines
        )
        total_retention = sum(line.retention for line in group.iva_lines)
        total_amount = sum(
            line.total if line.total is not None else line.base_amount
            for line in group.iva_lines
        )

        # --- Create Invoice ---
        try:
            with transaction.atomic():
                # Check for duplicate
                if Invoice.objects.filter(
                    provider=provider,
                    invoice_number=invoice_number,
                ).exists():
                    result.skipped += 1
                    result.rows.append(ImportRowResult(
                        row_number=row_num,
                        invoice_number=invoice_number,
                        provider_cif=cif,
                        status="skipped",
                        message=f"Duplicate (already exists) — {num_iva_lines} IVA lines",
                    ))
                    continue

                invoice = Invoice(
                    invoice_number=invoice_number,
                    date_issued=group.date_issued,
                    provider=provider,
                    company=company,
                    gross_amount=total_base,
                    total_amount=total_amount,
                    image_hash="",
                    needs_manual_review=True,
                    ocr_processed=True,
                )
                invoice.save()

                # Create one InvoiceItem per IVA line
                invoice_ct = ContentType.objects.get_for_model(Invoice)

                for order, line in enumerate(group.iva_lines):
                    if line.base_amount:
                        desc = group.description or "Imported line item"
                        iva_label = str(line.iva_percentage).rstrip('0').rstrip('.')
                        InvoiceItem.objects.create(
                            invoice=invoice,
                            description=f"{desc} (IVA {iva_label}%)",
                            quantity=Decimal("1"),
                            unit_price=line.base_amount,
                        )

                    # Apply IVA tax for this line
                    if iva_tax_type and line.iva_percentage > 0:
                        computed_iva = line.iva_amount
                        if computed_iva is None:
                            computed_iva = (
                                line.base_amount * line.iva_percentage / Decimal("100")
                            ).quantize(Decimal("0.01"))

                        AppliedTax.objects.create(
                            tax_type=iva_tax_type,
                            selected_percentage=line.iva_percentage,
                            base_amount=line.base_amount,
                            calculated_amount=computed_iva,
                            content_type=invoice_ct,
                            object_id=invoice.pk,
                            order=order,
                        )

                # Apply IRPF retention (aggregate across all lines)
                if irpf_tax_type and total_retention and total_retention > 0:
                    irpf_pct = Decimal("0")
                    if total_base > 0:
                        irpf_pct = (
                            total_retention / total_base * Decimal("100")
                        ).quantize(Decimal("0.01"))

                    AppliedTax.objects.create(
                        tax_type=irpf_tax_type,
                        selected_percentage=irpf_pct,
                        base_amount=total_base,
                        calculated_amount=total_retention,
                        content_type=invoice_ct,
                        object_id=invoice.pk,
                        order=len(group.iva_lines),
                    )

                # Recalculate totals
                invoice.recalculate_totals(save=True)

                result.created += 1
                result.rows.append(ImportRowResult(
                    row_number=row_num,
                    invoice_number=invoice_number,
                    provider_cif=cif,
                    status="created",
                    message=f"OK — {num_iva_lines} IVA lines",
                ))

        except IntegrityError as e:
            result.skipped += 1
            result.rows.append(ImportRowResult(
                row_number=row_num,
                invoice_number=invoice_number,
                provider_cif=cif,
                status="skipped",
                message=f"Duplicate: {e}",
            ))
        except Exception as e:
            result.errors += 1
            result.rows.append(ImportRowResult(
                row_number=row_num,
                invoice_number=invoice_number,
                provider_cif=cif,
                status="error",
                message=str(e),
            ))
