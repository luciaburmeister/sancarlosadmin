"""Fields exposed to DjangoQL search for sales invoices."""

from django.db.models import Q

from general.djangoql import SearchFields, flag, register
from invoices.djangoql_fields.invoice import IS_PAID
from invoices.models import (
    InvoicePaymentStatus,
    SalesInvoice,
    SalesInvoiceItem,
    SalesInvoiceStatus,
)
from payments.mixins import overdue_q

IS_FINAL = flag(
    "is_final",
    lambda: Q(status=SalesInvoiceStatus.FINAL),
    "Issued for real: not a proforma, not cancelled.",
)

IS_OVERDUE = flag(
    "is_overdue",
    lambda: overdue_q()
    & ~Q(payment_status=InvoicePaymentStatus.PAID)
    & Q(status=SalesInvoiceStatus.FINAL),
    "Past its due date (or with no due date at all) and not fully paid; "
    "a proforma is never overdue.",
)


@register(SalesInvoice)
class SalesInvoiceSearchFields(SearchFields):
    fields = (
        "id",
        "invoice_number",
        "incremental_id",
        "status",
        "date_issued",
        "due_date",
        "payment_commitment_date",
        "created_at",
        "updated_at",
        "subtotal_amount",
        "discount_amount",
        "tax_amount",
        "total_amount",
        "payment_status",
        "payment_method",
        "payment_terms_days",
        "is_external",
        "reference",
        "notes",
        "client",
        "company",
        "project_file",
        "project_files",
        "items",
        IS_PAID,
        IS_OVERDUE,
        IS_FINAL,
    )


@register(SalesInvoiceItem)
class SalesInvoiceItemSearchFields(SearchFields):
    fields = (
        "id",
        "description",
        "quantity",
        "unit_price",
        "discount_percentage",
        "discount_amount",
        "line_total",
        "reference",
        "quote_name",
        "source_id",
        "project_file",
    )
