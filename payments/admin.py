from django.contrib import admin
from django.contrib.contenttypes.models import ContentType
from django.utils.translation import gettext_lazy as _
from unfold.admin import ModelAdmin, TabularInline
from unfold.decorators import display

from core.admin.company_admin import CompanyRestrictedAdmin
from payments.models import Payment, PaymentAllocation, PaymentStatus


class PaymentAllocationInline(TabularInline):
    model = PaymentAllocation
    extra = 0
    fields = ("content_type", "object_id", "amount_applied")


@admin.register(Payment)
class PaymentAdmin(CompanyRestrictedAdmin, ModelAdmin):
    list_display = (
        "id",
        "payment_date",
        "company",
        "direction",
        "get_status_label",
        "method",
        "amount",
        "is_allocated",
        "reference",
    )
    list_filter = ("company", "direction", "status", "method", "payment_date")
    search_fields = ("reference", "notes")
    list_select_related = ("company",)
    readonly_fields = (
        "amount_pending_allocation",
        "amount_pending_allocation_currency",
    )
    inlines = [PaymentAllocationInline]
    ordering = ("-payment_date", "-id")

    @display(description=_("Allocated"), boolean=True)
    def is_allocated(self, obj):
        return obj.amount_pending_allocation.amount == 0

    @display(
        description=_("Status"),
        ordering="status",
        label={
            PaymentStatus.DRAFT: "warning",
            PaymentStatus.CONFIRMED: "info",
            PaymentStatus.RECONCILED: "success",
            PaymentStatus.CANCELLED: "danger",
        },
    )
    def get_status_label(self, obj):
        return obj.status