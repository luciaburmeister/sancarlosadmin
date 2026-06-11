from django.contrib import admin
from django.shortcuts import redirect
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from unfold.admin import ModelAdmin, TabularInline
from unfold.decorators import action, display
from unfold.contrib.filters.admin import RangeDateFilter
from core.admin.company_admin import CompanyRestrictedAdmin

from .models import TaxScheduleRule, TaxPeriod, TaxPayment, TaxPeriodStatus
from .signals import advance_all_rules


# ─────────────────────────────────────────────────────────────────────────────
# Inlines
# ─────────────────────────────────────────────────────────────────────────────

class TaxPaymentInline(TabularInline):
    model = TaxPayment
    extra = 0
    tab = True
    fields = ["payment_date", "amount_paid", "reference", "notes"]
    verbose_name = _("Pago")
    verbose_name_plural = _("Pagos")


# ─────────────────────────────────────────────────────────────────────────────
# TaxScheduleRule
# ─────────────────────────────────────────────────────────────────────────────

@admin.register(TaxScheduleRule)
class TaxScheduleRuleAdmin(CompanyRestrictedAdmin, ModelAdmin):
    list_display        = ["name", "tax_type", "get_frequency_label", "payment_day", "meses_hasta_vencimiento", "start_date", "company", "get_active_label"]
    list_filter         = ["company", "is_active", "period_unit"]
    search_fields       = ["name", "tax_type__tax_name"]
    autocomplete_fields = ["company", "tax_type"]
    actions_row = ["view_periods_action"]

    fieldsets = (
        (_("General"), {
            "fields": (
                "name",
                ("company", "tax_type"),
                "start_date",
                "is_active",
            ),
        }),
        (_("Definición del período"), {
            "description": _(
                "Define la duración de cada período de acumulación. "
                "Al guardar, el primer período se crea automáticamente desde la fecha de inicio."
            ),
            "fields": (
                ("period_length", "period_unit"),
            ),
        }),
        (_("Vencimiento del pago"), {
            "description": _(
                "Define cuándo vence el pago después de que cierre el período."
            ),
            "fields": (
                ("payment_day", "meses_hasta_vencimiento"),
            ),
        }),
    )

    @action(description=_("Ver períodos"), icon="date_range")
    def view_periods_action(self, request, object_id: int):
        url = reverse("admin:finance_taxperiod_changelist") + f"?rule__id__exact={object_id}"
        return redirect(url)

    @display(description=_("Frecuencia"))
    def get_frequency_label(self, obj):
        return f"Cada {obj.period_length} {obj.get_period_unit_display().lower()}"

    @display(
        description=_("Activa"),
        label={True: "success", False: "danger"},
    )
    def get_active_label(self, obj):
        return obj.is_active


# ─────────────────────────────────────────────────────────────────────────────
# TaxPeriod
# ─────────────────────────────────────────────────────────────────────────────

@admin.register(TaxPeriod)
class TaxPeriodAdmin(CompanyRestrictedAdmin, ModelAdmin):
    list_display        = ["rule", "date_from", "date_to", "due_date", "tax_collected", "tax_paid", "tax_net", "net_after_credit", "get_status_label", "company"]
    list_filter         = ["company", "status", ("date_from", RangeDateFilter), "rule"]
    search_fields       = ["rule__name", "rule__tax_type__tax_name"]
    autocomplete_fields = ["company", "rule"]
    readonly_fields     = ["tax_collected", "tax_paid", "tax_net", "accumulated_credit", "net_after_credit"]

    inlines        = [TaxPaymentInline]
    actions_detail = ["compute_action", "mark_pending_action"]
    actions_row    = ["compute_row_action"]

    fieldsets = (
        (_("Período"), {
            "fields": (
                ("company", "rule"),
                ("date_from", "date_to", "due_date"),
                "status",
                "notes",
            ),
        }),
        (_("Resumen fiscal"), {
            "description": _(
                "Usa la acción 'Calcular' para acumular los importes desde las facturas. "
                "Impuesto repercutido = ventas · Impuesto soportado = compras · Impuesto a ingresar = diferencia."
            ),
            "fields": (
                ("tax_collected", "tax_paid", "tax_net"),
                ("accumulated_credit", "net_after_credit"),
            ),
        }),
    )

    @display(
        description=_("Estado"),
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

    @action(description=_("Calcular importes"), icon="calculate")
    def compute_action(self, request, object_id: int):
        period = TaxPeriod.objects.get(pk=object_id)
        period.compute(save=True)
        self.message_user(
            request,
            _(f"Repercutido: {period.tax_collected} · Soportado: {period.tax_paid} · A ingresar: {period.tax_net}")
        )
        return redirect(reverse("admin:finance_taxperiod_change", args=[object_id]))

    @action(description=_("Calcular"), icon="calculate")
    def compute_row_action(self, request, object_id: int):
        period = TaxPeriod.objects.get(pk=object_id)
        period.compute(save=True)
        self.message_user(request, _("Importes calculados correctamente."))
        return redirect(reverse("admin:finance_taxperiod_changelist"))

    @action(description=_("Marcar como pendiente de pago"), icon="schedule")
    def mark_pending_action(self, request, object_id: int):
        period = TaxPeriod.objects.get(pk=object_id)
        if period.status == TaxPeriodStatus.OPEN:
            period.status = TaxPeriodStatus.PENDING_PAYMENT
            period.save(update_fields=["status"])
            self.message_user(request, _("Período marcado como pendiente de pago."))
        else:
            self.message_user(request, _("Solo los períodos abiertos pueden marcarse como pendientes."), level="warning")
        return redirect(reverse("admin:finance_taxperiod_change", args=[object_id]))

    def get_queryset(self, request):
        # Advance periods and flag overdue on every list load
        advance_all_rules()
        return super().get_queryset(request)


# ─────────────────────────────────────────────────────────────────────────────
# TaxPayment
# ─────────────────────────────────────────────────────────────────────────────

@admin.register(TaxPayment)
class TaxPaymentAdmin(CompanyRestrictedAdmin, ModelAdmin):
    list_display        = ["period", "payment_date", "amount_paid", "reference", "company"]
    list_filter         = ["company", ("payment_date", RangeDateFilter)]
    search_fields       = ["period__rule__name", "reference"]
    autocomplete_fields = ["company", "period"]

    fieldsets = (
        (_("Detalles del pago"), {
            "fields": (
                ("company", "period"),
                ("payment_date", "amount_paid"),
                "reference",
                "notes",
            ),
        }),
    )