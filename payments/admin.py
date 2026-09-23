from django.contrib import admin
from django.contrib.contenttypes.models import ContentType
from django.urls import reverse
from django.utils.html import format_html
from django.utils.translation import gettext_lazy as _
from unfold.admin import ModelAdmin, StackedInline, TabularInline
from unfold.decorators import display

from banking.models import BankReconciliation
from core.admin.company_admin import CompanyRestrictedAdmin
from payments.mixins import unallocated_payments_q
from payments.models import Payment, PaymentAllocation, PaymentStatus


class UnallocatedFilter(admin.SimpleListFilter):
    """
    Payments with money still to allocate, by the same rule a client's credit
    is summed with, so the credit figure links to exactly the payments behind it.
    """

    title = _("Pending allocation")
    parameter_name = "unallocated"

    def lookups(self, request, model_admin):
        return [("yes", _("Yes")), ("no", _("No"))]

    def queryset(self, request, queryset):
        if self.value() == "yes":
            return queryset.filter(unallocated_payments_q())
        if self.value() == "no":
            return queryset.exclude(unallocated_payments_q())
        return queryset


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
        "amount_pending_allocation",
        "is_allocated",
        "client",
        "reference",
    )
    list_filter = (
        "company", "direction", "status", "method", UnallocatedFilter, "payment_date",
    )
    search_fields = ("reference", "notes", "client__name", "client__commercial_name")
    list_select_related = ("company", "client")
    autocomplete_fields = ("client",)
    readonly_fields = (
        "amount_pending_allocation",
        "amount_pending_allocation_currency",
    )
    inlines = [PaymentAllocationInline, PaymentBankReconciliationInline]
    ordering = ("-payment_date", "-id")

    def save_related(self, request, form, formsets, change):
        super().save_related(request, form, formsets, change)
        # Editing the amount alone (no allocation touched) would otherwise
        # leave the stored pending figure, and the client's credit, stale.
        form.instance.recalculate_pending_allocation()

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