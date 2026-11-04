"""
Draft (pro forma) sales invoices built from free-text lines.

Used by the public API: the caller sends the client and the lines, each with
the taxes that apply to it (any configured ``TaxType`` at one of its rates: a
VAT adds, a retention such as IRPF subtracts). The invoice is created as a
PROFORMA (``SalesInvoice.save`` assigns the pro forma number and the client's
default due date); numbering, journal entries and e-invoicing happen when
someone finalizes it in the ERP.

Taxes are stored like the other creation paths store them: one invoice-level
``AppliedTax`` per (tax type, rate) on the sum of the totals of the lines that
carry it, then ``recalculate_totals`` computes each amount, the tax and the
total.
"""
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from invoices.models import AppliedTax, SalesInvoice, SalesInvoiceItem, SalesInvoiceStatus, TaxType
from invoices.services.sales_invoice_payment import resolve_sales_invoice_payment_method


@dataclass(frozen=True)
class LineTax:
    tax_type: TaxType
    rate: Decimal


@dataclass(frozen=True)
class DraftLine:
    description: str
    quantity: Decimal
    unit_price: Decimal
    discount_percentage: Decimal = Decimal("0")
    taxes: tuple[LineTax, ...] = field(default_factory=tuple)


def check_line_tax(tax: LineTax) -> None:
    """An active tax type, at one of its rates (a type listing no rates takes any)."""
    tax_type = tax.tax_type
    if not tax_type.is_active:
        raise ValidationError(f"Tax type {tax_type.pk} ({tax_type.tax_name}) is not active.")
    options = {Decimal(str(option)) for option in (tax_type.percentage_options or [])}
    if options and tax.rate not in options:
        allowed = ", ".join(str(option) for option in sorted(options))
        raise ValidationError(f"Rate {tax.rate} is not allowed for {tax_type.tax_name} ({allowed}).")


def _check_lines(lines: list[DraftLine]) -> None:
    if not lines:
        raise ValidationError({"lines": "At least one line is required."})
    for index, line in enumerate(lines):
        type_ids = [tax.tax_type.pk for tax in line.taxes]
        if len(type_ids) != len(set(type_ids)):
            raise ValidationError({"lines": f"Line {index + 1} has the same tax type more than once."})
        for tax in line.taxes:
            try:
                check_line_tax(tax)
            except ValidationError as exc:
                raise ValidationError({"lines": f"Line {index + 1}: {' '.join(exc.messages)}"}) from exc


def create_draft_sales_invoice(
    *,
    company,
    client,
    lines: list[DraftLine],
    date_issued: date | None = None,
    due_date: date | None = None,
    payment_method: str | None = None,
    notes: str = "",
    reference: str = "",
) -> SalesInvoice:
    """
    A PROFORMA ``SalesInvoice`` of ``company`` for ``client`` with ``lines``.

    Raises ``ValidationError`` when the client belongs to another company,
    there are no lines, or a line's tax is inactive, repeated or at a rate its
    type doesn't allow.
    """
    if client.company_id != company.pk:
        raise ValidationError({"client_id": "The client does not belong to this company."})
    _check_lines(lines)

    with transaction.atomic():
        invoice = SalesInvoice.objects.create(
            company=company,
            client=client,
            status=SalesInvoiceStatus.PROFORMA,
            invoice_number=None,
            date_issued=date_issued or timezone.localdate(),
            due_date=due_date,
            payment_method=resolve_sales_invoice_payment_method(client=client, explicit=payment_method),
            notes=notes,
            reference=reference or None,
        )

        # (tax type, rate) → [tax type, rate, base], in first-seen order
        bases: dict[tuple[int, Decimal], list] = {}
        for line in lines:
            item = SalesInvoiceItem.objects.create(
                invoice=invoice,
                description=line.description,
                quantity=line.quantity,
                unit_price=line.unit_price,
                discount_percentage=line.discount_percentage,
            )
            line_total = Decimal(str(item.line_total.amount)) if item.line_total else Decimal("0.00")
            for tax in line.taxes:
                entry = bases.setdefault((tax.tax_type.pk, tax.rate), [tax.tax_type, tax.rate, Decimal("0.00")])
                entry[2] += line_total

        ct = ContentType.objects.get_for_model(invoice)
        for order, (tax_type, rate, base) in enumerate(bases.values()):
            AppliedTax.objects.create(
                content_type=ct, object_id=invoice.pk, tax_type=tax_type,
                selected_percentage=rate, base_amount=base, order=order,
            )

        invoice.recalculate_totals(save=True)
    return invoice
