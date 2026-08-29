"""Reusable admin inlines for documents that receive payments.

Any `Payable` document (Invoice, SalesInvoice, ExpenseClaim, ...) can drop
`DocumentPaymentsInline` into its `inlines` to get a read-only breakdown of the
payments allocated to it. The reverse view - the allocations belonging to a
single Payment - lives in `payments.admin`.
"""
from django.urls import reverse
from django.utils.html import format_html
from django.utils.translation import gettext_lazy as _
from unfold.admin import GenericTabularInline

from payments.models import PaymentAllocation


class DocumentPaymentsInline(GenericTabularInline):
    model = PaymentAllocation
    ct_field = "content_type"
    fk_field = "object_id"
    extra = 0
    tab = True
    verbose_name = _("Payment")
    verbose_name_plural = _("Payments")
    fields = ("get_payment_date", "get_method", "get_reference", "amount_applied", "get_status", "get_payment_link")
    readonly_fields = ("get_payment_date", "get_method", "get_reference", "amount_applied", "get_status", "get_payment_link")
    can_delete = False

    def get_payment_date(self, obj):
        return obj.payment.payment_date
    get_payment_date.short_description = _("Payment Date")

    def get_payment_link(self, obj):
        from payments.models import PaymentCreatedBy

        url = reverse("admin:payments_payment_change", args=[obj.payment_id])
        label = _("Edit") if obj.payment.created_by == PaymentCreatedBy.MANUAL else _("View")
        return format_html('<a href="{}" class="text-primary-600 underline">{}</a>', url, label)
    get_payment_link.short_description = _("Payment")

    def get_method(self, obj):
        return obj.payment.get_method_display()
    get_method.short_description = _("Method")

    def get_reference(self, obj):
        return obj.payment.reference or "—"
    get_reference.short_description = _("Reference")

    def get_status(self, obj):
        return obj.payment.get_status_display()
    get_status.short_description = _("Status")

    def get_queryset(self, request):
        return super().get_queryset(request).select_related("payment")
