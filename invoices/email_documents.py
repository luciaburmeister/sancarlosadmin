from django.utils.translation import gettext_lazy as _

from general.email import EmailDocument, register


@register
class SalesInvoiceEmail(EmailDocument):
    key = "sales_invoice"
    label = _("Sales Invoice")
    track_delivery = True
    track_opens = True


@register
class PaymentReminderEmail(EmailDocument):
    key = "payment_reminder"
    label = _("Payment Reminder")
    track_delivery = True
    track_opens = True


@register
class PaymentReminderReportEmail(EmailDocument):
    key = "payment_reminder_report"
    label = _("Payment Reminder Report")


@register
class PendingInvoicesDigestEmail(EmailDocument):
    key = "pending_invoices_digest"
    label = _("Pending Invoices Digest")


@register
class UnmatchedPODigestEmail(EmailDocument):
    key = "unmatched_po_digest"
    label = _("Unmatched Purchase Order Digest")


@register
class DuplicateInvoiceAlertEmail(EmailDocument):
    key = "duplicate_invoice_alert"
    label = _("Duplicate Invoice Alert")
