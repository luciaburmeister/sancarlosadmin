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


def import_invoices_from_excel(file_obj: BinaryIO, default_year: int | None = None) -> ImportResult:
    """
    Import invoices from an Excel file with the generic format.

    Args:
        file_obj: The uploaded Excel file.
        default_year: Year to use for dates without year (e.g., DD/MM format).

    Returns:
        ImportResult with summary and per-row details.
    """
    result = ImportResult()

    wb = openpyxl.load_workbook(file_obj, read_only=True, data_only=True)
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

    # Cache tax types
    iva_tax_type = _get_iva_tax_type()
    irpf_tax_type = _get_irpf_tax_type()

    # Cache companies and providers for efficiency
    company_cache: dict[str, Company | None] = {}
    provider_cache: dict[tuple[str, int], Provider | None] = {}

    def get_cell(row, col_name):
        idx = col_map.get(col_name)
        if idx is not None and idx < len(row):
            return row[idx].value
        return None

    for row in ws.iter_rows(min_row=2, max_row=ws.max_row):
        result.total_rows += 1
        if row[0].value is None:
            continue
        row_num = row[0].row

        invoice_number = str(get_cell(row, "invoice_number") or "").strip()
        if not invoice_number:
            result.skipped += 1
            result.rows.append(ImportRowResult(
                row_number=row_num,
                status="skipped",
                message="Empty invoice number",
            ))
            continue

        # --- Company ---
        company_name = str(get_cell(row, "company") or "").strip()
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
        cif = _clean_cif(get_cell(row, "provider_cif"))
        provider_name = str(get_cell(row, "provider_name") or "").strip()

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
        date_issued = _parse_date(get_cell(row, "date_issued"), default_year=default_year)

        # --- Amounts ---
        base_amount = _parse_decimal(get_cell(row, "base_amount")) or Decimal("0.00")
        iva_pct = _parse_decimal(get_cell(row, "iva_percentage")) or Decimal("0")
        iva_amount = _parse_decimal(get_cell(row, "iva_amount"))
        retention = _parse_decimal(get_cell(row, "retention")) or Decimal("0")
        total = _parse_decimal(get_cell(row, "total"))
        description = str(get_cell(row, "description") or "").strip()

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

    wb.close()
    return result
