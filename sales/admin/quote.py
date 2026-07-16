from decimal import Decimal

from django.contrib import admin
from django.shortcuts import redirect
from django.urls import reverse, path
from django.utils.html import format_html
from django.utils.translation import gettext_lazy as _
from djmoney.money import Money
from import_export.admin import ImportExportModelAdmin
from unfold.admin import ModelAdmin, StackedInline, TabularInline
from unfold.admin import GenericTabularInline as _GenericTabularInline
from unfold.decorators import action, display
from unfold.contrib.filters.admin import (
    AutocompleteSelectMultipleFilter,
    MultipleChoicesDropdownFilter,
    RangeDateFilter,
)
from unfold.contrib.import_export.forms import ImportForm, SelectableFieldsExportForm

from configurator.admin.configuration import AppliedTaxInline
from configurator.commission.admin import QuoteCommissionInline
from configurator.commission.services import CommissionService
from configurator.models import Configuration
from core.admin.company_admin import CompanyRestrictedAdmin
from core.admin.email_log import EmailLogInline
from core.admin.xlsx_export import FormattedXlsxExportMixin
from invoices.models import AppliedTax
from management.views import QuoteAcceptanceWizardView
from sales.forms import QuoteItemImportForm, QuoteItemConfirmImportForm
from sales.models import (
    Quote, QuoteItem, QuoteInventoryItem, QuoteStatus,
    QuoteConfigurationGroup, QuotePhoto, QuotePhotoCategory,
)
from sales.resources import QuoteItemImportResource, QuoteExportResource
from sales.views.quote_dashboard_view import QuoteDashboardView
from sales.views.quote_export_view import QuoteExportView
from sales.views.quote_send_email_view import QuoteSendEmailView
from sales.views_analysis import ConversionAnalysisView

from .mixins import _CompanyImportMixin


class QuotePhotoInline(TabularInline):
    model = QuotePhoto
    extra = 0
    tab = True
    tab_title = _("Photos")
    fields = ["image", "category", "caption", "uploaded_at"]
    readonly_fields = ["uploaded_at"]
    autocomplete_fields = ["category"]

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        if db_field.name == "category":
            import re
            match = re.search(r'/admin/sales/quote/(\d+)/change/', request.path)
            if match:
                quote = Quote.objects.select_related("company").get(pk=match.group(1))
                kwargs["queryset"] = QuotePhotoCategory.objects.filter(
                    company=quote.company, is_active=True
                ).order_by("order")
            else:
                kwargs["queryset"] = QuotePhotoCategory.objects.none()
        return super().formfield_for_foreignkey(db_field, request, **kwargs)


@admin.register(QuotePhotoCategory)
class QuotePhotoCategoryAdmin(CompanyRestrictedAdmin, ModelAdmin):
    list_display = ["name", "order", "is_active", "company"]
    list_filter = ["company", "is_active"]
    search_fields = ["name"]
    autocomplete_fields = ["company"]
    list_editable = ["order", "is_active"]


class QuoteItemInline(TabularInline):
    model = QuoteItem
    extra = 1
    tab = True
    autocomplete_fields = ["material"]
    readonly_fields = ["line_total", "cost_display", "margin_display"]
    fields = ["material", "quantity", "unit_price", "discount", "line_total", "cost_display", "margin_display"]

    @display(description=_("Coste"))
    def cost_display(self, obj):
        if not obj.pk or not obj.material_id:
            return "—"
        cost = obj.material.cost_per_unit
        if not cost or cost.amount == 0:
            return "—"
        return cost

    @display(description=_("Margen"))
    def margin_display(self, obj):
        if not obj.pk or not obj.material_id:
            return "—"
        cost = obj.material.cost_per_unit
        if not cost or cost.amount == 0 or not obj.unit_price or obj.unit_price.amount == 0:
            return "—"
        disc = (Decimal("100") - obj.discount) / Decimal("100")
        sale = obj.unit_price.amount * disc
        if sale == 0:
            return "—"
        margin = (sale - cost.amount) / sale * 100
        color = "#22c55e" if margin >= 0 else "#ef4444"
        margin_str = f"{float(margin):.1f}%"
        return format_html('<span style="color:{};font-weight:600;">{}</span>', color, margin_str)


class QuoteInventoryItemInline(TabularInline):
    model = QuoteInventoryItem
    extra = 0
    tab = True
    autocomplete_fields = ["inventory_item"]
    readonly_fields = ["unit_price", "line_total", "total_with_tax", "cost_display", "margin_display", "margin_pct_display"]
    fields = ["inventory_item", "quantity", "discount", "unit_price", "cost_display", "line_total", "total_with_tax", "margin_display", "margin_pct_display"]
    verbose_name = _("Sale item")
    verbose_name_plural = _("Sale items")

    def _get_item_cost(self, obj):
        """Last purchase price for this inventory item, fallback to cost_price."""
        from purchasing.models import PurchaseOrderItem
        if not obj.inventory_item_id:
            return Decimal("0")
        last_po = (
            PurchaseOrderItem.objects
            .filter(inventory_item=obj.inventory_item)
            .order_by("-purchase_order__date_ordered")
            .values_list("unit_price", flat=True)
            .first()
        )
        if last_po:
            return last_po
        cp = obj.inventory_item.cost_price
        if cp and cp.amount:
            return cp.amount
        return Decimal("0")

    @display(description=_("Cost"))
    def cost_display(self, obj):
        unit_cost = self._get_item_cost(obj)
        return Money(unit_cost * (obj.quantity or Decimal("0")), "EUR")

    @display(description=_("Margin (€)"))
    def margin_display(self, obj):
        revenue = obj.line_total.amount if obj.line_total else Decimal("0")
        cost = self._get_item_cost(obj) * (obj.quantity or Decimal("0"))
        return Money(revenue - cost, "EUR")

    @display(description=_("Margin (%)"))
    def margin_pct_display(self, obj):
        revenue = obj.line_total.amount if obj.line_total else Decimal("0")
        cost = self._get_item_cost(obj) * (obj.quantity or Decimal("0"))
        if revenue:
            pct = ((revenue - cost) / revenue * 100).quantize(Decimal("0.1"))
            return f"{pct}%"
        return "—"

class QuoteConfigurationGroupInline(StackedInline):
    model = QuoteConfigurationGroup
    extra = 1
    tab = True
    ordering_field= "order"
    hide_ordering_field=True
    tab_title = _("Configuration Groups")
    fields = ["name", "notes", "order"]
    ordering = ["order"]


@admin.register(QuoteConfigurationGroup)
class QuoteConfigurationGroupAdmin(ModelAdmin):
    list_display = ["name", "quote", "order"]
    search_fields = ["name"]
    autocomplete_fields = ["quote"]
    list_filter = ["quote"]


class ConfigurationReadOnlyInline(TabularInline):
    model = Configuration
    extra = 0
    tab = True
    readonly_fields = [
        "config_link", "status",
        "cost_display", "margin_display", "margin_pct_display","subtotal_price", "total_price_display",
    ]
    fields = [
        "config_link", "status", "quote_group",
        "cost_display",
        "margin_display", "margin_pct_display",
        "subtotal_price", "total_price_display",
    ]
    autocomplete_fields = ["quote_group"]
    can_delete = True
    max_num = 0

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        if db_field.name == "quote_group":
            import re
            match = re.search(r'/admin/sales/quote/(\d+)/change/', request.path)
            if match:
                kwargs["queryset"] = QuoteConfigurationGroup.objects.filter(
                    quote_id=match.group(1)
                ).order_by("order")
            else:
                kwargs["queryset"] = QuoteConfigurationGroup.objects.none()
        return super().formfield_for_foreignkey(db_field, request, **kwargs)

    @display(description=_("Name"))
    def config_link(self, obj):
        url = reverse("admin:configurator_configuration_change", args=[obj.pk])
        return format_html('<a href="{}" target="_blank">{}</a>', url, obj.name)

    @display(description=_("Total price"))
    def total_price_display(self, obj):
        return obj.total_price

    @display(description=_("Cost"))
    def cost_display(self, obj):
        return obj.total_cost

    @display(description=_("Margin (€)"))
    def margin_display(self, obj):
        cost = obj.total_cost.amount if obj.total_cost else Decimal("0")
        revenue = obj.subtotal_price.amount if obj.subtotal_price else Decimal("0")
        return Money(revenue - cost, "EUR")

    @display(description=_("Margin (%)"))
    def margin_pct_display(self, obj):
        cost = obj.total_cost.amount if obj.total_cost else Decimal("0")
        revenue = obj.subtotal_price.amount if obj.subtotal_price else Decimal("0")
        if revenue:
            pct = ((revenue - cost) / revenue * 100).quantize(Decimal("0.1"))
            return f"{pct}%"
        return "—"


@admin.register(Quote)
class QuoteAdmin(CompanyRestrictedAdmin, FormattedXlsxExportMixin, ImportExportModelAdmin, ModelAdmin):
    resource_classes = [QuoteExportResource]
    import_form_class = ImportForm
    export_form_class = SelectableFieldsExportForm
    list_display = ["id", "project_name", "client", "date_sent","date_accepted", "project_file", "show_status_customized_color", "total_price_display", "company"]
    list_filter = [
        ("status",MultipleChoicesDropdownFilter),
        ("company",AutocompleteSelectMultipleFilter),
        ("created_at",RangeDateFilter),
        ("date_sent", RangeDateFilter),
        ("date_accepted",RangeDateFilter)]
    search_fields = ["project_name", "client__name", "id", "external_id"]
    autocomplete_fields = ["client", "agent", "project_file", "company","discount_policies"]
    readonly_fields = [
        "subtotal_price", "tax_amount", "total_price", "tax_breakdown",
        "discount_amount", "discount_breakdown", "date_sent", "date_accepted", "date_rejected",
        "total_cost", "total_commissions_display",
        "margin_before_discount_display", "margin_before_discount_pct_display",
        "margin_after_discount_display", "margin_after_discount_pct_display",
    ]
    list_select_related = ["client", "project_file__client", "company"]
    list_filter_submit = True
    change_form_outer_after_template = "sales/quote_preset_picker.html"

    inlines = [ConfigurationReadOnlyInline,
    QuoteInventoryItemInline,
    QuoteCommissionInline,
    QuoteConfigurationGroupInline,
    AppliedTaxInline,
    EmailLogInline,
    QuotePhotoInline,
    ]


    actions_list = [ "quote_dashboard_action"]
    actions_row = ["add_configuration_to_quote", "export_quote_action", "send_email_row_action"]
    actions_detail = ["send_email_detail_action"]

    fieldsets = (
        ("Quote Details", {
            "classes": ["tab"],
            "fields": (
                ("client", "agent"),
                ("status", "company"),
                ("project_name",),
                ("date", "expires_at"),
                ("subtotal_price", "discount_amount"),
                ("tax_amount", "total_price"),
                "discount_policies",
            )
        }),
        (_("Details"), {
            "classes": ["tab"],
            "fields": (
                ("project_file", "external_id"),
                "observations",
                "discount_breakdown",
                "tax_breakdown",
            )
        }),
        (_("Status Dates"), {
            "classes": ["tab"],
            "fields": (
                ("date_sent", "date_accepted", "date_rejected"),
            )
        }),
        (_("Margin"), {
            "classes": ["tab"],
            "fields": (
                ("total_cost", "total_commissions_display"),
                ("margin_before_discount_display", "margin_before_discount_pct_display"),
                ("margin_after_discount_display", "margin_after_discount_pct_display"),
            )
        }),
    )

    @display(description=_("Total price"))
    def total_price_display(self, obj):
        return obj.total_price

    # ── Margin tab display methods ────────────────────────────────────

    def _get_quote_cost(self, obj):
        """Read the stored total_cost from the quote."""
        if obj.total_cost and hasattr(obj.total_cost, 'amount'):
            return obj.total_cost.amount
        return Decimal("0")

    def _get_total_commissions(self, obj):
        return sum(
            (c.commission_amount.amount for c in obj.commissions.all() if c.commission_amount),
            Decimal("0"),
        )

    @display(description=_("Total Commissions"))
    def total_commissions_display(self, obj):
        return Money(self._get_total_commissions(obj), "EUR")

    def _get_pre_discount_revenue(self, obj):
        """Revenue before any discounts: sum of (unit_price × qty) for all items."""
        inv_item_base = sum(
            (item.unit_price.amount * item.quantity for item in obj.inventory_items.all()),
            Decimal("0"),
        )
        quote_item_base = sum(
            (item.unit_price.amount * item.quantity for item in obj.items.all()),
            Decimal("0"),
        )
        config_subtotal = sum(
            (c.subtotal_price.amount for c in obj.configurations.all()),
            Decimal("0"),
        )
        return inv_item_base + quote_item_base + config_subtotal

    @display(description=_("Margin before discount (€)"))
    def margin_before_discount_display(self, obj):
        revenue = self._get_pre_discount_revenue(obj)
        cost = self._get_quote_cost(obj)
        commissions = self._get_total_commissions(obj)
        return Money(revenue - cost - commissions, "EUR")

    @display(description=_("Margin before discount (%)"))
    def margin_before_discount_pct_display(self, obj):
        revenue = self._get_pre_discount_revenue(obj)
        cost = self._get_quote_cost(obj)
        commissions = self._get_total_commissions(obj)
        total_cost = cost + commissions
        if total_cost:
            pct = ((revenue - total_cost) / total_cost * 100).quantize(Decimal("0.1"))
            return f"{pct}%"
        return "—"

    @display(description=_("Margin after discount (€)"))
    def margin_after_discount_display(self, obj):
        revenue = getattr(obj, 'discounted_subtotal', None)
        if revenue is None or not hasattr(revenue, 'amount'):
            revenue_amt = (obj.subtotal_price.amount if obj.subtotal_price else Decimal("0"))
        else:
            revenue_amt = revenue.amount
        cost = self._get_quote_cost(obj)
        commissions = self._get_total_commissions(obj)
        return Money(revenue_amt - cost - commissions, "EUR")

    @display(description=_("Margin after discount (%)"))
    def margin_after_discount_pct_display(self, obj):
        revenue = getattr(obj, 'discounted_subtotal', None)
        if revenue is None or not hasattr(revenue, 'amount'):
            revenue_amt = (obj.subtotal_price.amount if obj.subtotal_price else Decimal("0"))
        else:
            revenue_amt = revenue.amount
        cost = self._get_quote_cost(obj)
        commissions = self._get_total_commissions(obj)
        total_cost = cost + commissions
        if total_cost:
            pct = ((revenue_amt - total_cost) / total_cost * 100).quantize(Decimal("0.1"))
            return f"{pct}%"
        return "—"

    @display(
        description=_("Status"),
        ordering="status",
        label={
            _(QuoteStatus.ACCEPTED.label): "success",
            _(QuoteStatus.SENT.label): "info",
            _(QuoteStatus.REJECTED.label): "warning",
            _(QuoteStatus.DRAFT.label): "primary",
        },
    )
    def show_status_customized_color(self, obj):
        return _(obj.get_status_display())

    @action(description=_("Add Configuration"))
    def add_configuration_to_quote(self, request, object_id: int):
        url = reverse("admin:configurator_configuration_wizard_select")
        return redirect(f"{url}?quote_id={object_id}")

    def save_related(self, request, form, formsets, change):
        super().save_related(request, form, formsets, change)
        form.instance.recalc_totals()
        CommissionService.prepopulate_for_quote(form.instance)
        CommissionService.recalculate(form.instance)

    def change_view(self, request, object_id, form_url="", extra_context=None):
        from sales.forms import QuotePresetPickerForm
        extra_context = extra_context or {}
        extra_context["preset_picker_form"] = QuotePresetPickerForm()
        return super().change_view(request, object_id, form_url, extra_context)

    def get_urls(self):
        urls = super().get_urls()
        custom = [
            path(
                "conversion-analysis/",
                self.admin_site.admin_view(
                    ConversionAnalysisView.as_view(model_admin=self)
                ),
                name="sales_quote_conversion_analysis",
            ),
            path(
                "dashboard/",
                self.admin_site.admin_view(
                    QuoteDashboardView.as_view(model_admin=self)
                ),
                name="sales_quote_dashboard",
            ),
            path(
                "<int:quote_id>/accept-wizard/",
                self.admin_site.admin_view(QuoteAcceptanceWizardView.as_view(model_admin=self)),
                name="sales_quote_acceptance_wizard",
            ),
            path(
                "<int:quote_id>/export/",
                self.admin_site.admin_view(QuoteExportView.as_view(model_admin=self)),
                name="sales_quote_export",
            ),
            path(
                "<int:quote_id>/send-email/",
                self.admin_site.admin_view(QuoteSendEmailView.as_view(model_admin=self)),
                name="sales_quote_send_email",
            ),
        ]
        return custom + urls

    @action(description=_("Export Quote"), attrs={"target": "_blank"})
    def export_quote_action(self, request, object_id: int):
        return redirect(reverse("admin:sales_quote_export", args=[object_id]))

    @action(
        description=_("Quote Dashboard"),
        url_path="quote-dashboard-action",
        permissions=["quote_dashboard"],
        icon="bar_chart",
    )
    def quote_dashboard_action(self, request):
        return redirect(reverse("admin:sales_quote_dashboard"))

    def has_quote_dashboard_permission(self, request):
        return request.user.has_perm("sales.can_view_quote_dashboard")

    def response_change(self, request, obj):
        if obj.status == "accepted" and not obj.project_file:
            return redirect("admin:sales_quote_acceptance_wizard", quote_id=obj.pk)
        return super().response_change(request, obj)

    def response_add(self, request, obj, post_url_continue=None):
        if obj.status == "accepted" and not obj.project_file:
            return redirect("admin:sales_quote_acceptance_wizard", quote_id=obj.pk)
        return super().response_add(request, obj, post_url_continue)

    @action(description=_("Send Email"), icon="mail")
    def send_email_row_action(self, request, object_id):
        url = reverse("admin:sales_quote_send_email", args=[object_id])
        return redirect(f"{url}?redirect_to=changelist")

    @action(description=_("Send Email"), icon="mail")
    def send_email_detail_action(self, request, object_id):
        return redirect(reverse("admin:sales_quote_send_email", args=[object_id]))


@admin.register(QuoteItem)
class QuoteItemAdmin(_CompanyImportMixin, ImportExportModelAdmin, ModelAdmin):
    resource_classes = [QuoteItemImportResource]
    import_form_class = QuoteItemImportForm

    list_display = ["quote_name", "material_name", "unit_price", "discount", "line_total", "cost_display", "margin_display"]
    search_fields = ["quote__project_name", "material__reference_code", "material__name"]
    autocomplete_fields = ["quote", "material", "category"]
    readonly_fields = ["line_total", "cost_display"]
    list_select_related = ["quote", "material"]

    def get_confirm_form_class(self, request):
        return QuoteItemConfirmImportForm

    @display(description=_("Quote"))
    def quote_name(self, obj):
        return obj.quote.project_name if obj.quote_id else "-"

    @display(description=_("Material"))
    def material_name(self, obj):
        return obj.material.name if obj.material_id else "-"

    @display(description=_("Coste"))
    def cost_display(self, obj):
        if not obj.material_id:
            return "—"
        cost = obj.material.cost_per_unit
        if not cost or cost.amount == 0:
            return "—"
        return cost

    @display(description=_("Margen"))
    def margin_display(self, obj):
        if not obj.material_id:
            return "—"
        cost = obj.material.cost_per_unit
        if not cost or cost.amount == 0 or not obj.unit_price or obj.unit_price.amount == 0:
            return "—"
        disc = (Decimal("100") - obj.discount) / Decimal("100")
        sale = obj.unit_price.amount * disc
        if sale == 0:
            return "—"
        margin = (sale - cost.amount) / sale * 100
        color = "#22c55e" if margin >= 0 else "#ef4444"
        margin_str = f"{float(margin):.1f}%"
        return format_html('<span style="color:{};font-weight:600;">{}</span>', color, margin_str)


class QuoteInventoryItemAppliedTaxInline(_GenericTabularInline):
    model = AppliedTax
    extra = 0
    autocomplete_fields = ("tax_type",)
    ordering = ("order",)
    verbose_name = _("Applied tax")
    verbose_name_plural = _("Applied taxes")
    tab = True

    def get_queryset(self, request):
        return super().get_queryset(request).select_related("tax_type")


@admin.register(QuoteInventoryItem)
class QuoteInventoryItemAdmin(ModelAdmin):
    list_display = ["quote_name", "inventory_item_name", "unit_price", "discount", "line_total"]
    search_fields = ["quote__project_name", "inventory_item__code", "inventory_item__name"]
    autocomplete_fields = ["quote", "inventory_item"]
    readonly_fields = ["line_total"]
    list_select_related = ["quote", "inventory_item"]
    inlines = [QuoteInventoryItemAppliedTaxInline]

    @display(description=_("Quote"))
    def quote_name(self, obj):
        return obj.quote.project_name if obj.quote_id else "-"

    @display(description=_("Inventory Item"))
    def inventory_item_name(self, obj):
        return str(obj.inventory_item) if obj.inventory_item_id else "-"
