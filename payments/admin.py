from django.contrib import admin
from django.contrib.contenttypes.models import ContentType
from django.urls import reverse
from django.utils.html import format_html
from django.utils.translation import gettext_lazy as _
from unfold.admin import ModelAdmin, StackedInline, TabularInline
from unfold.decorators import display

from banking.models import BankReconciliation
from core.admin.company_admin import CompanyRestrictedAdmin
from payments.models import Payment, PaymentAllocation, PaymentStatus


class PaymentAllocationInline(TabularInline):
    model = PaymentAllocation
    extra = 0
    fields = ("content_type", "object_id", "amount_applied")


class PaymentBankReconciliationInline(StackedInline):
    """Read-only inline showing the bank transaction this payment was matched to."""
    model = BankReconciliation
    fk_name = "payment"
    extra = 0
    max_num = 0
    can_delete = False
    tab = True
    fields = (
        "transaction_link",
        "is_confirmed",
        "method",
        "unmatched_amount",
        "confirmed_at",
    )
    readonly_fields = (
        "transaction_link",
        "is_confirmed",
        "method",
        "unmatched_amount",
        "confirmed_at",
    )

    def has_add_permission(self, request, obj=None):
        return False

    @admin.display(description=_("Bank Transaction"))
    def transaction_link(self, obj):
        if not obj.pk or not obj.bank_transaction_id:
            return "—"
        txn = obj.bank_transaction
        url = reverse("admin:banking_banktransaction_change", args=[txn.pk])
        return format_html(
            '<a href="{}" style="color: var(--primary-600, #0369a1); font-weight: 600;">{}</a>',
            url, str(txn),
        )


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
    inlines = [PaymentAllocationInline, PaymentBankReconciliationInline]
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