from django import forms
from django.contrib import admin, messages
from django.contrib.contenttypes.models import ContentType
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect
from django.urls import path, reverse
from django.utils.html import format_html, format_html_join
from django.utils.translation import gettext_lazy as _
from unfold.admin import ModelAdmin, TabularInline
from unfold.contrib.filters.admin import AutocompleteSelectMultipleFilter
from unfold.decorators import display
from core.admin.company_admin import CompanyRestrictedAdmin
from payments.models import PaymentAllocation

from .models import TaxPeriod, TaxPeriodStatus
from .models_planned_tax_payment import PlannedTaxPayment, TaxInstallment, TaxInstallmentPaymentStatus


def tax_period_links(tax_periods) -> str:
    periods = list(tax_periods)
    if not periods:
        return "—"
    return format_html_join(
        ", ",
        '<a href="{}" class="text-primary-600 underline">{}</a>',
        (
            (reverse("admin:finance_taxperiod_change", args=[period.pk]), str(period))
            for period in periods
        ),
    )


def planned_tax_payment_links(planned_payments) -> str:
    payments = list(planned_payments)
    if not payments:
        return "—"
    return format_html_join(
        ", ",
        '<a href="{}" class="text-primary-600 underline">{}</a>',
        (
            (reverse("admin:finance_plannedtaxpayment_change", args=[payment.pk]), str(payment))
            for payment in payments
        ),
    )


class PlannedTaxPaymentForm(forms.ModelForm):
    class Meta:
        model = PlannedTaxPayment
        fields = "__all__"

    def clean(self):
        cleaned_data = super().clean()
        tax_schedule_rule = cleaned_data.get("tax_schedule_rule")
        tax_periods = cleaned_data.get("tax_periods")
        if tax_periods:
            if not tax_schedule_rule:
                raise forms.ValidationError(
                    _("Tax periods can only be linked when a tax schedule rule is selected.")
                )
            mismatched = tax_periods.exclude(rule=tax_schedule_rule)
            if mismatched.exists():
                raise forms.ValidationError(
                    _("All linked tax periods must belong to the selected tax schedule rule.")
                )
        return cleaned_data


class TaxInstallmentInline(TabularInline):
    """
    Editable installment rows, shown as part of the main form. A plan
    always has at least one installment - a single-row plan is just a
    lump-sum payment entered as one installment.
    """
    model = TaxInstallment
    verbose_name = _("Installment")
    verbose_name_plural = _("Installments")
    prefix = "installments_edit"
    extra = 1
    tab = False
    fields = ("due_date", "amount")


PAYMENT_STATUS_LABELS = {
    TaxInstallmentPaymentStatus.PAID: "success",
    TaxInstallmentPaymentStatus.PARTIAL: "warning",
    TaxInstallmentPaymentStatus.UNPAID: "danger",
}


class TaxInstallmentReadOnlyInline(TabularInline):
    """Read-only view of the same installments, for reviewing payment status."""
    model = TaxInstallment
    verbose_name = _("Tax Installment")
    verbose_name_plural = _("Tax Installments")
    prefix = "installments_view"
    extra = 0
    tab = True
    fields = ("due_date", "amount", "get_payment_status", "amount_paid", "get_bank_transaction_link", "mark_paid_button")
    readonly_fields = fields

    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    @display(description=_("Payment Status"), label=PAYMENT_STATUS_LABELS)
    def get_payment_status(self, obj):
        return obj.payment_status

    def get_bank_transaction_link(self, obj):
        from banking.models import BankReconciliation

        if not obj.pk:
            return "—"
        ct = ContentType.objects.get_for_model(TaxInstallment)
        allocations = PaymentAllocation.objects.filter(
            content_type=ct, object_id=obj.pk
        ).select_related("payment")

        links = []
        for allocation in allocations:
            try:
                recon = allocation.payment.reconciliation
            except BankReconciliation.DoesNotExist:
                continue
            if not recon.bank_transaction_id:
                continue
            url = reverse("admin:banking_banktransaction_change", args=[recon.bank_transaction_id])
            links.append(format_html('<a href="{}" class="text-primary-600 underline">{}</a>', url, str(recon.bank_transaction)))

        if not links:
            return "—"
        return format_html_join(", ", "{}", ((link,) for link in links))
    get_bank_transaction_link.short_description = _("Bank Transaction")

    @admin.display(description=_("Actions"))
    def mark_paid_button(self, obj):
        if not obj.pk or obj.payment_status == TaxInstallmentPaymentStatus.PAID:
            return "—"
        url = reverse("admin:finance_plannedtaxpayment_mark_installment_paid", args=[obj.pk])
        return format_html(
            '<a href="{}" class="font-medium text-primary-600 dark:text-primary-400 hover:underline" '
            'onclick="return confirm(\'{}\');">'
            '<span class="material-symbols-outlined" style="font-size:16px;vertical-align:middle;">price_check</span>'
            ' {}</a>',
            url,
            _("Mark this installment as paid without a bank transaction?"),
            _("Mark as paid"),
        )


@admin.register(PlannedTaxPayment)
class PlannedTaxPaymentAdmin(CompanyRestrictedAdmin, ModelAdmin):
    form = PlannedTaxPaymentForm
    list_display = ["description", "tax_type", "direction", "get_installments_count", "get_installments_total", "get_payment_status", "company"]
    list_filter = (
        ("company", AutocompleteSelectMultipleFilter),
        ("tax_type", AutocompleteSelectMultipleFilter),
        "direction",
    )
    search_fields = ["description", "tax_type__tax_name"]
    list_filter_submit = True
    autocomplete_fields = ["company", "tax_type", "tax_schedule_rule"]
    filter_horizontal = ["tax_periods"]
    readonly_fields = ["get_payment_status", "get_calculated_reference"]
    inlines = [TaxInstallmentInline, TaxInstallmentReadOnlyInline]

    fieldsets = (
        (_("Planned Payment"), {
            "fields": (
                "tax_schedule_rule",
                ("company", "tax_type"),
                "description",
                "direction",
                "tax_periods",
            ),
        }),
        (_("Status"), {
            "fields": (
                "get_payment_status",
                "get_calculated_reference",
                "notes",
                "drag_forward_if_unpaid",
            ),
        }),
    )

    @display(description=_("Payment Status"), label=PAYMENT_STATUS_LABELS)
    def get_payment_status(self, obj):
        return obj.payment_status

    def get_readonly_fields(self, request, obj=None):
        fields = list(self.readonly_fields)
        if obj and obj.tax_schedule_rule_id:
            fields += ["company", "tax_type"]
        return fields

    def formfield_for_manytomany(self, db_field, request, **kwargs):
        if db_field.name == "tax_periods":
            object_id = request.resolver_match.kwargs.get("object_id") if request.resolver_match else None
            rule_id = None
            linked_ids = []
            if object_id:
                planned = PlannedTaxPayment.objects.filter(pk=object_id).only("tax_schedule_rule_id").first()
                if planned:
                    rule_id = planned.tax_schedule_rule_id
                    linked_ids = list(planned.tax_periods.values_list("pk", flat=True))
            else:
                rule_id = request.GET.get("tax_schedule_rule")
            if rule_id:
                kwargs["queryset"] = TaxPeriod.objects.filter(rule_id=rule_id).filter(
                    ~Q(status=TaxPeriodStatus.PAID) | Q(pk__in=linked_ids)
                ).order_by("-date_from")
            else:
                kwargs["queryset"] = TaxPeriod.objects.none()
            formfield = super().formfield_for_manytomany(db_field, request, **kwargs)
            formfield.help_text = db_field.help_text
            return formfield
        return super().formfield_for_manytomany(db_field, request, **kwargs)

    def get_tax_periods_link(self, obj):
        return tax_period_links(obj.tax_periods.all())
    get_tax_periods_link.short_description = _("Tax Periods")

    def get_tax_periods_count(self, obj):
        count = obj.tax_periods.count()
        if not count:
            return "—"
        return _("%(count)d period(s)") % {"count": count}
    get_tax_periods_count.short_description = _("Tax Periods")

    def get_installments_count(self, obj):
        count = obj.installments.count()
        if not count:
            return "—"
        return _("%(count)d installment(s)") % {"count": count}
    get_installments_count.short_description = _("Installments")

    def get_installments_total(self, obj):
        installments = list(obj.installments.all())
        if not installments:
            return "—"
        total = sum(installment.amount.amount for installment in installments)
        return f"{total} {installments[0].amount.currency}"
    get_installments_total.short_description = _("Total")

    def get_calculated_reference(self, obj):
        if not obj or not obj.pk:
            return "—"
        from .models import TaxPeriod

        amount = TaxPeriod.suggested_settlement_amount(obj.tax_periods.all())
        return f"{amount} EUR"
    get_calculated_reference.short_description = _("Calculated reference for linked periods")

    def save_related(self, request, form, formsets, change):
        old_period_ids = set(form.instance.tax_periods.values_list("pk", flat=True)) if change else set()
        super().save_related(request, form, formsets, change)
        new_period_ids = set(form.instance.tax_periods.values_list("pk", flat=True))
        TaxPeriod.recalculate_status_for(old_period_ids | new_period_ids)

    def get_urls(self):
        custom_urls = [
            path(
                "installment/<int:installment_id>/mark-paid/",
                self.admin_site.admin_view(self.mark_installment_paid),
                name="finance_plannedtaxpayment_mark_installment_paid",
            ),
        ]
        return custom_urls + super().get_urls()

    def mark_installment_paid(self, request, installment_id):
        installment = get_object_or_404(
            TaxInstallment.objects.select_related("planned_tax_payment__company"),
            pk=installment_id,
        )
        planned = installment.planned_tax_payment
        redirect_url = reverse("admin:finance_plannedtaxpayment_change", args=[planned.pk]) + "#installments"

        if not self.has_change_permission(request, planned):
            messages.error(request, _("You do not have permission to modify this payment."))
            return redirect(redirect_url)

        remaining = installment.amount_remaining
        if installment.payment_status == TaxInstallmentPaymentStatus.PAID or remaining.amount <= 0:
            messages.info(request, _("Installment due %(date)s is already paid.") % {
                "date": installment.due_date,
            })
            return redirect(redirect_url)

        from django.db import transaction as db_transaction
        from payments.models import Payment, PaymentCreatedBy, PaymentDirection, PaymentMethod, PaymentStatus

        with db_transaction.atomic():
            payment = Payment.objects.create(
                company=planned.company,
                payment_date=installment.due_date,
                amount=remaining.amount,
                amount_currency=str(remaining.currency),
                direction=PaymentDirection.INBOUND if planned.is_refund else PaymentDirection.OUTBOUND,
                method=PaymentMethod.CASH,
                status=PaymentStatus.CONFIRMED,
                created_by=PaymentCreatedBy.MANUAL,
                notes=_("Marked as paid manually from admin (no bank transaction). %(installment)s") % {
                    "installment": str(installment),
                },
            )
            PaymentAllocation.objects.create(
                payment=payment,
                amount_applied=remaining.amount,
                amount_applied_currency=str(remaining.currency),
                content_type=ContentType.objects.get_for_model(TaxInstallment),
                object_id=installment.pk,
            )

        messages.success(
            request,
            _("Installment due %(date)s marked as paid (%(amount)s).") % {
                "date": installment.due_date,
                "amount": remaining,
            },
        )
        return redirect(redirect_url)
