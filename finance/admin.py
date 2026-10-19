from urllib.parse import urlencode, urlparse

from django.contrib import admin
from django.shortcuts import redirect
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from unfold.admin import ModelAdmin
from unfold.decorators import action, display
from unfold.contrib.filters.admin import (
    RangeDateFilter,
    ChoicesDropdownFilter,
    AutocompleteSelectMultipleFilter,
)
from core.admin.company_admin import CompanyRestrictedAdmin

from .admin_planned_tax_payment import planned_tax_payment_links
from .models import TaxScheduleRule, TaxPeriod, TaxPeriodStatus, PaymentCalendar
from .models import advance_all_rules


@admin.register(TaxScheduleRule)
class TaxScheduleRuleAdmin(CompanyRestrictedAdmin, ModelAdmin):
    list_display        = ["name", "tax_type", "get_frequency_label", "payment_day", "meses_hasta_vencimiento", "start_date", "company", "get_active_label"]
    list_filter         = (
        ("company", AutocompleteSelectMultipleFilter),
        "is_active",
        ("period_unit", ChoicesDropdownFilter),
    )
    search_fields       = ["name", "tax_type__tax_name"]
    list_filter_submit  = True
    autocomplete_fields = ["company", "tax_type"]
    actions_row         = ["view_periods_action", "create_planned_payment_action"]
    actions_detail      = ["create_planned_payment_action"]

    fieldsets = (
        (_("General"), {
            "fields": (
                "name",
                ("company", "tax_type"),
                "start_date",
                "is_active",
            ),
        }),
        (_("Period Definition"), {
            "description": _(
                "Define how long each accumulation period lasts. "
                "On save, the first period is created automatically from the start date."
            ),
            "fields": (
                ("period_length", "period_unit"),
            ),
        }),
        (_("Payment Deadline"), {
            "description": _(
                "Define when payment is due after the period closes."
            ),
            "fields": (
                ("payment_day", "meses_hasta_vencimiento"),
            ),
        }),
    )

    @action(description=_("View Periods"), icon="date_range")
    def view_periods_action(self, request, object_id: int):
        rule = TaxScheduleRule.objects.get(pk=object_id)
        rule.compute_all_periods()
        url = reverse("admin:finance_taxperiod_changelist") + f"?rule__id__exact={object_id}"
        return redirect(url)

    @display(description=_("Frequency"))
    def get_frequency_label(self, obj):
        return _("Every %(length)s %(unit)s") % {"length": obj.period_length, "unit": obj.get_period_unit_display().lower()}

    @display(
        description=_("Active"),
        label={True: "success", False: "danger"},
    )
    def get_active_label(self, obj):
        return obj.is_active

    @action(description=_("Create planned payment"), icon="receipt_long", permissions=["create_planned_payment"])
    def create_planned_payment_action(self, request, object_id: int):
        rule = TaxScheduleRule.objects.get(pk=object_id)
        params = [
            ("tax_schedule_rule", rule.pk),
            ("tax_type", rule.tax_type_id),
            ("company", rule.company_id),
        ]
        url = reverse("admin:finance_plannedtaxpayment_add")
        return redirect(f"{url}?{urlencode(params)}")

    def has_create_planned_payment_permission(self, request, object_id=None):
        return request.user.has_perm("finance.add_plannedtaxpayment")


@admin.register(TaxPeriod)
class TaxPeriodAdmin(CompanyRestrictedAdmin, ModelAdmin):
    list_display        = ["rule", "date_from", "date_to", "due_date", "tax_collected", "tax_paid", "tax_net", "net_after_credit", "get_status_label", "company"]
    list_filter          = (
        ("company", AutocompleteSelectMultipleFilter),
        ("status", ChoicesDropdownFilter),
        ("date_from", RangeDateFilter),
        ("rule", AutocompleteSelectMultipleFilter),
    )
    search_fields       = ["rule__name", "rule__tax_type__tax_name"]
    list_filter_submit  = True
    autocomplete_fields = ["company", "rule"]
    readonly_fields     = ["tax_collected", "tax_paid", "tax_net", "accumulated_credit", "net_after_credit", "get_planned_payments_link"]
    actions_list        = ["export_excel_action", "compute_all_action"]
    actions             = ["create_planned_payment_action"]

    fieldsets = (
        (_("Period"), {
            "fields": (
                ("company", "rule"),
                ("date_from", "date_to", "due_date"),
                "status",
                "notes",
            ),
        }),
        (_("Tax Summary"), {
            "description": _(
                "Amounts are updated automatically every hour via a background task. "
                "Use 'Compute All' to force a refresh immediately. "
                "Tax collected = sales · Tax paid = purchases · Net = difference."
            ),
            "fields": (
                ("tax_collected", "tax_paid", "tax_net"),
                ("accumulated_credit", "net_after_credit"),
            ),
        }),
        (_("Planned Payments"), {
            "fields": (
                "get_planned_payments_link",
            ),
        }),
    )

    def get_planned_payments_link(self, obj):
        return planned_tax_payment_links(obj.planned_payments.all())
    get_planned_payments_link.short_description = _("Planned Payments")

    @display(
        description=_("Status"),
        ordering="status",
        label={
            TaxPeriodStatus.OPEN:            "info",
            TaxPeriodStatus.PENDING_PAYMENT: "warning",
            TaxPeriodStatus.PAID:            "success",
            TaxPeriodStatus.OVERDUE:         "danger",
        },
    )
    def get_status_label(self, obj):
        return obj.status

    @admin.action(description=_("Create planned payment"))
    def create_planned_payment_action(self, request, queryset):
        periods = list(queryset.select_related("rule", "rule__tax_type").order_by("date_from"))
        if not periods:
            return

        rule_ids = {p.rule_id for p in periods}
        if len(rule_ids) > 1:
            self.message_user(
                request,
                _("Select periods from the same tax schedule rule."),
                level="error",
            )
            return

        first = periods[0]
        last = max(periods, key=lambda p: p.due_date)
        suggested_amount = TaxPeriod.suggested_settlement_amount(periods)

        params = [
            ("tax_schedule_rule", first.rule_id),
            ("tax_type", first.rule.tax_type_id),
            ("company", first.company_id),
            ("payment_date", last.due_date.isoformat()),
            ("amount", f"{suggested_amount} {first.tax_net.currency}"),
            ("description", f"{first.rule.tax_type} {first.date_from} - {last.date_to}"),
        ]
        params += [("tax_periods", period.pk) for period in periods]

        url = reverse("admin:finance_plannedtaxpayment_add")
        return redirect(f"{url}?{urlencode(params)}")

    def get_urls(self):
        from django.urls import path
        from .views import TaxPeriodExportView
        urls = super().get_urls()
        custom = [
            path(
                "export/",
                self.admin_site.admin_view(TaxPeriodExportView.as_view()),
                name="finance_taxperiod_export",
            ),
        ]
        return custom + urls

    @action(description=_("Export"), icon="download")
    def export_excel_action(self, request):
        params = ""
        referer = request.META.get("HTTP_REFERER")
        if referer:
            params = urlparse(referer).query

        url = reverse("admin:finance_taxperiod_export")
        if params:
            url = f"{url}?{params}"
        return redirect(url)

    @action(description=_("Compute All Periods"), icon="calculate")
    def compute_all_action(self, request):
        total = 0
        for rule in TaxScheduleRule.objects.filter(is_active=True):
            rule.compute_all_periods()
            total += rule.periods.count()
        self.message_user(request, _(f"All {total} periods computed in chronological order."))

        referer = request.META.get("HTTP_REFERER")
        if referer:
            return redirect(referer)
        return redirect(reverse("admin:finance_taxperiod_changelist"))

    def get_queryset(self, request):
        advance_all_rules()
        return super().get_queryset(request)


@admin.register(PaymentCalendar)
class PaymentCalendarAdmin(CompanyRestrictedAdmin, ModelAdmin):

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def get_urls(self):
        from django.urls import path
        from .views_calendar import PaymentCalendarExportView
        urls = super().get_urls()
        custom = [
            path(
                "export/",
                self.admin_site.admin_view(PaymentCalendarExportView.as_view()),
                name="finance_paymentcalendar_export",
            ),
        ]
        return custom + urls

    def changelist_view(self, request, extra_context=None):
        from .views_calendar import PaymentCalendarView
        view = PaymentCalendarView.as_view(model_admin=self)
        return view(request)