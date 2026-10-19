from django import forms
from django.contrib import admin
from django.db.models import Q
from django.shortcuts import redirect
from django.urls import reverse
from django.utils.html import format_html_join
from django.utils.translation import gettext_lazy as _
from unfold.admin import ModelAdmin
from unfold.decorators import action
from unfold.contrib.filters.admin import (
    RangeDateFilter,
    ChoicesDropdownFilter,
    AutocompleteSelectMultipleFilter,
)
from core.admin.company_admin import CompanyRestrictedAdmin
from payments.admin_inlines import DocumentPaymentsInline

from .models import TaxPeriod, TaxPeriodStatus
from .models_planned_tax_payment import PlannedTaxPayment


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


@admin.register(PlannedTaxPayment)
class PlannedTaxPaymentAdmin(CompanyRestrictedAdmin, ModelAdmin):
    form = PlannedTaxPaymentForm
    list_display = ["description", "tax_type", "amount", "payment_date", "is_paid", "paid_via", "get_tax_periods_count", "company"]
    list_filter = (
        ("company", AutocompleteSelectMultipleFilter),
        ("tax_type", AutocompleteSelectMultipleFilter),
        ("payment_date", RangeDateFilter),
        "is_paid",
        ("paid_via", ChoicesDropdownFilter),
    )
    search_fields = ["description", "tax_type__tax_name"]
    list_filter_submit = True
    autocomplete_fields = ["company", "tax_type", "tax_schedule_rule"]
    filter_horizontal = ["tax_periods"]
    readonly_fields = ["is_paid", "paid_via", "paid_at", "paid_by", "get_calculated_reference"]
    actions = ["mark_selected_as_paid", "mark_selected_as_unpaid"]
    actions_row = ["mark_paid_action", "mark_unpaid_action"]
    actions_detail = [
        {
            "title": _("Payment"),
            "items": ["mark_paid_action", "mark_unpaid_action"],
        },
    ]
    inlines = [DocumentPaymentsInline]

    fieldsets = (
        (_("Planned Payment"), {
            "fields": (
                "tax_schedule_rule",
                ("company", "tax_type"),
                "tax_periods",
                "get_calculated_reference",
                "description",
                ("amount", "payment_date"),
                "notes",
            ),
        }),
        (_("Status"), {
            "fields": (
                ("is_paid", "paid_via"),
                ("paid_at", "paid_by"),
            ),
        }),
    )

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

    def get_calculated_reference(self, obj):
        if not obj or not obj.pk:
            return "—"
        from .models import TaxPeriod

        amount = TaxPeriod.suggested_settlement_amount(obj.tax_periods.all())
        currency = obj.amount.currency if obj.amount else "EUR"
        return f"{amount} {currency}"
    get_calculated_reference.short_description = _("Calculated reference for linked periods")

    def save_related(self, request, form, formsets, change):
        old_period_ids = set(form.instance.tax_periods.values_list("pk", flat=True)) if change else set()
        super().save_related(request, form, formsets, change)
        new_period_ids = set(form.instance.tax_periods.values_list("pk", flat=True))
        TaxPeriod.recalculate_status_for(old_period_ids | new_period_ids)

    @action(description=_("Mark as paid"), icon="paid", permissions=["mark_paid"])
    def mark_paid_action(self, request, object_id: int):
        planned = PlannedTaxPayment.objects.get(pk=object_id)
        planned.mark_as_paid(request.user)
        self.message_user(request, _("Payment marked as paid."))
        return redirect(reverse("admin:finance_plannedtaxpayment_change", args=[object_id]))

    def has_mark_paid_permission(self, request, object_id=None):
        return request.user.has_perm("finance.change_plannedtaxpayment")

    @action(description=_("Mark as unpaid"), icon="money_off", permissions=["mark_unpaid"])
    def mark_unpaid_action(self, request, object_id: int):
        planned = PlannedTaxPayment.objects.get(pk=object_id)
        if planned.amount_remaining <= 0:
            self.message_user(
                request,
                _("Cannot mark as unpaid: bank reconciliation still covers this amount."),
                level="warning",
            )
        else:
            planned.mark_as_unpaid()
            self.message_user(request, _("Payment marked as unpaid."))
        return redirect(reverse("admin:finance_plannedtaxpayment_change", args=[object_id]))

    def has_mark_unpaid_permission(self, request, object_id=None):
        return request.user.has_perm("finance.change_plannedtaxpayment")

    @admin.action(description=_("Mark selected payments as paid"))
    def mark_selected_as_paid(self, request, queryset):
        updated = 0
        for planned in queryset.filter(is_paid=False):
            planned.mark_as_paid(request.user)
            updated += 1
        self.message_user(request, _("%(n)d payment(s) marked as paid.") % {"n": updated})

    @admin.action(description=_("Mark selected payments as unpaid"))
    def mark_selected_as_unpaid(self, request, queryset):
        updated = 0
        skipped = 0
        for planned in queryset.filter(is_paid=True):
            if planned.amount_remaining <= 0:
                skipped += 1
                continue
            planned.mark_as_unpaid()
            updated += 1
        self.message_user(
            request,
            _("%(n)d marked as unpaid, %(s)d skipped (still covered by reconciliation).")
            % {"n": updated, "s": skipped},
        )
