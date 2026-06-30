from collections import defaultdict
from dataclasses import dataclass, field
from django.utils.translation import gettext_lazy as _

from invoices.models import Invoice, InvoicePaymentStatus


@dataclass
class BulkInvoicePaymentRow:
    invoice_number: str
    status: str = ""  # "updated", "skipped", "not_found", "ambiguous"
    message: str = ""


@dataclass
class BulkInvoicePaymentResult:
    total: int = 0
    updated: int = 0
    skipped: int = 0
    not_found: int = 0
    ambiguous: int = 0
    target_status: str = ""
    rows: list = field(default_factory=list)


def bulk_update_invoice_payment_status(
    company, invoice_numbers: list[str], target_status: str
) -> BulkInvoicePaymentResult:
    """
    Set payment_status to *target_status* for a list of purchase invoices
    belonging to *company*. Returns per-row detail.

    Invoice.invoice_number is only unique per provider (unique_together =
    ['provider', 'invoice_number']), not per company. If a number matches
    more than one provider within the same company, the row is reported as
    "ambiguous" and left untouched rather than guessing which one to update.
    """
    result = BulkInvoicePaymentResult(
        total=len(invoice_numbers),
        target_status=target_status,
    )

    label = dict(InvoicePaymentStatus.choices).get(target_status, target_status)

    # Pre-fetch all matching invoices in one query, grouped by invoice_number
    # since more than one (different providers) can share the same number.
    invoices_qs = Invoice.objects.filter(
        company=company,
        invoice_number__in=invoice_numbers,
    ).select_related("provider")

    invoices_by_number = defaultdict(list)
    for inv in invoices_qs:
        invoices_by_number[inv.invoice_number].append(inv)

    to_update = []

    for number in invoice_numbers:
        matches = invoices_by_number.get(number, [])

        if not matches:
            result.not_found += 1
            result.rows.append(BulkInvoicePaymentRow(
                invoice_number=number,
                status="not_found",
                message=str(_("Invoice not found for this company.")),
            ))
        elif len(matches) > 1:
            result.ambiguous += 1
            providers = ", ".join(sorted({str(inv.provider) for inv in matches}))
            result.rows.append(BulkInvoicePaymentRow(
                invoice_number=number,
                status="ambiguous",
                message=str(_(
                    "Matches multiple providers (%(providers)s). Skipped — "
                    "use the admin to update these individually."
                ) % {"providers": providers}),
            ))
        else:
            invoice = matches[0]
            if invoice.payment_status == target_status:
                result.skipped += 1
                result.rows.append(BulkInvoicePaymentRow(
                    invoice_number=number,
                    status="skipped",
                    message=str(_("Already %(status)s.") % {"status": label}),
                ))
            else:
                invoice.payment_status = target_status
                to_update.append(invoice)
                result.updated += 1
                result.rows.append(BulkInvoicePaymentRow(
                    invoice_number=number,
                    status="updated",
                    message=str(_("Set to %(status)s.") % {"status": label}),
                ))

    # Bulk update in one query
    if to_update:
        Invoice.objects.bulk_update(to_update, ["payment_status"])

    return result