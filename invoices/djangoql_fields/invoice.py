"""Fields exposed to DjangoQL search for purchase invoices."""

from django.db.models import Q

from general.djangoql import SearchFields, flag, register
from invoices.models import (
    Invoice,
    InvoiceDocumentType,
    InvoiceItem,
    InvoicePaymentStatus,
)
from payments.mixins import overdue_q

IS_PAID = flag(
    "is_paid",
    lambda: Q(payment_status=InvoicePaymentStatus.PAID),
    "Fully paid.",
)

IS_OVERDUE = flag(
    "is_overdue",
    lambda: overdue_q() & ~Q(payment_status=InvoicePaymentStatus.PAID),
    "Past its due date (or with no due date at all) and not fully paid.",
)


@register(Invoice)
class InvoiceSearchFields(SearchFields):
    fields = (
        "id",
        "invoice_number",
        "document_type",
        "date_issued",
        "accounting_date",
        "due_date",
        "created_at",
        "updated_at",
        "net_amount",
        "total_amount",
        "currency",
        "payment_status",
        "payment_method",
        "confirmation_status",
        "needs_manual_review",
        "is_total_balanced",
        "ocr_processed",
        "ocr_processing",
        "ocr_error",
        "observations",
        "provider",
        "company",
        "account",
        "project_file",
        "cost_center",
        "rectifies",
        "uploaded_by",
        "items",
        IS_PAID,
        IS_OVERDUE,
        flag(
            "is_credit_note",
            lambda: Q(document_type=InvoiceDocumentType.CREDIT_NOTE),
            "A credit note (factura rectificativa) rather than a plain invoice.",
        ),
        flag(
            "has_file",
            lambda: ~Q(file="") & Q(file__isnull=False),
            "The scanned invoice file is attached.",
        ),
        flag(
            "items_missing_account",
            lambda: Q(items__isnull=False) & Q(items__account__isnull=True),
            "At least one line has no accounting account assigned.",
        ),
    )


@register(InvoiceItem)
class InvoiceItemSearchFields(SearchFields):
    fields = (
        "id",
        "description",
        "quantity",
        "unit_price",
        "discount_percentage",
        "discount_amount",
        "gross_total",
        "line_total",
        "account",
    )
