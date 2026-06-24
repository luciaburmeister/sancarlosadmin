from dataclasses import dataclass, field
from django.utils.translation import gettext_lazy as _

from invoices.models import SalesInvoice, InvoicePaymentStatus


@dataclass
class BulkPaymentRow:
    invoice_number: str
    status: str = ""  # "updated", "skipped", "not_found"
    message: str = ""


@dataclass
class BulkPaymentResult:
    total: int = 0
    updated: int = 0
    skipped: int = 0
    not_found: int = 0
    target_status: str = ""
    rows: list = field(default_factory=list)


def bulk_update_payment_status(
    company, invoice_numbers: list[str], target_status: str
) -> BulkPaymentResult:
    """
    Set payment_status to *target_status* for a list of sales invoices
    belonging to *company*.  Returns per-row detail.
    """
    result = BulkPaymentResult(
        total=len(invoice_numbers),
        target_status=target_status,
    )

    label = dict(InvoicePaymentStatus.choices).get(target_status, target_status)

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
            result.rows.append(BulkPaymentRow(
                invoice_number=number,
                status="not_found",
                message=str(_("Invoice not found for this company.")),
            ))
        elif invoice.payment_status == target_status:
            result.skipped += 1
            result.rows.append(BulkPaymentRow(
                invoice_number=number,
                status="skipped",
                message=str(_("Already %(status)s.") % {"status": label}),
            ))
        else:
            invoice.payment_status = target_status
            to_update.append(invoice)
            result.updated += 1
            result.rows.append(BulkPaymentRow(
                invoice_number=number,
                status="updated",
                message=str(_("Set to %(status)s.") % {"status": label}),
            ))

    # Bulk update in one query
    if to_update:
        SalesInvoice.objects.bulk_update(to_update, ["payment_status"])

    return result
