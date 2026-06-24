from dataclasses import dataclass, field
from django.utils.translation import gettext_lazy as _

from invoices.models import SalesInvoice, InvoicePaymentStatus


@dataclass
class BulkMarkPaidRow:
    invoice_number: str
    status: str = ""  # "paid", "skipped", "not_found"
    message: str = ""


@dataclass
class BulkMarkPaidResult:
    total: int = 0
    marked: int = 0
    skipped: int = 0
    not_found: int = 0
    rows: list = field(default_factory=list)


def bulk_mark_invoices_paid(company, invoice_numbers: list[str]) -> BulkMarkPaidResult:
    """
    Mark a list of sales invoices as paid for a given company.
    Returns a result with per-row detail.
    """
    result = BulkMarkPaidResult(total=len(invoice_numbers))

    # Pre-fetch all matching invoices in one query
    invoices_qs = SalesInvoice.objects.filter(
        company=company,
        invoice_number__in=invoice_numbers,
    )
    invoices_by_number = {inv.invoice_number: inv for inv in invoices_qs}

    to_update = []

    for number in invoice_numbers:
        invoice = invoices_by_number.get(number)
        if invoice is None:
            result.not_found += 1
            result.rows.append(BulkMarkPaidRow(
                invoice_number=number,
                status="not_found",
                message=str(_("Invoice not found for this company.")),
            ))
        elif invoice.payment_status == InvoicePaymentStatus.PAID:
            result.skipped += 1
            result.rows.append(BulkMarkPaidRow(
                invoice_number=number,
                status="skipped",
                message=str(_("Already marked as paid.")),
            ))
        else:
            invoice.payment_status = InvoicePaymentStatus.PAID
            to_update.append(invoice)
            result.marked += 1
            result.rows.append(BulkMarkPaidRow(
                invoice_number=number,
                status="paid",
                message=str(_("Marked as paid.")),
            ))

    # Bulk update in one query
    if to_update:
        SalesInvoice.objects.bulk_update(to_update, ["payment_status"])

    return result
