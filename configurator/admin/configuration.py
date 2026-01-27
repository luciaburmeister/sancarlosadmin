from django.contrib import admin
from unfold.admin import ModelAdmin
from configurator.models import Configuration, ConfigurationParameterValue, ConfigurationItem, PricingRuleAssignment, ConfigurationFlow,ConfigurationStatus
from unfold.admin import TabularInline,GenericTabularInline
from unfold.contrib.filters.admin import AutocompleteSelectMultipleFilter,RangeDateTimeFilter,MultipleChoicesDropdownFilter
from django.urls import path
from configurator.views import ProductSelectionView, ConfigurationWizardView, ConfigurationExportView
from django.urls import reverse
from django.shortcuts import redirect
from unfold.decorators import action
from django.utils.translation import gettext_lazy as _
from django.http import HttpRequest
from invoices.models import AppliedTax
from unfold.decorators import display
from core.admin.company_admin import CompanyRestrictedAdmin

class AppliedTaxInline(GenericTabularInline):
    hide_ordering_field = True
    ordering_field = "order"
    model = AppliedTax
    extra = 0
    autocomplete_fields = ("tax_type",)
    ordering = ("order",)
    fields = ("tax_type", "selected_percentage", "order")
    verbose_name = _("Applied tax")
    verbose_name_plural = _("Applied taxes")
    tab = True
    def get_queryset(self, request):
        return super().get_queryset(request).select_related("tax_type")

class ParameterValueInline(TabularInline):
    hide_ordering_field = True
    ordering_field = "order"
    model = ConfigurationParameterValue
    extra = 0
    fields = ("parameter", "value","order")
    tab = True
    readonly_fields = ["parameter"]
    verbose_name = _("Parameter value")
    verbose_name_plural = _("Parameter values")
    
    def get_queryset(self, request):
        return super().get_queryset(request).select_related("parameter")
class ConfigurationItemInline(TabularInline):
    model = ConfigurationItem
    extra = 0
    fields = (
        "is_template_item",
        "component",
        "quantity",
        "length",
        "unit_price",
        "line_total",
    )
    readonly_fields = ("line_total", "is_template_item")
    tab = True
    autocomplete_fields = ["component"]
    verbose_name = _("Configuration item")
    verbose_name_plural = _("Configuration items")

    def get_queryset(self, request):
        return super().get_queryset(request).select_related("component__material")

class PricingRuleAssignmentInline(TabularInline):
    model = PricingRuleAssignment
    extra = 0
    autocomplete_fields = ["rule"]
    tab = True
    verbose_name = _("Pricing rule assignment")
    verbose_name_plural = _("Pricing rule assignments")

@admin.register(Configuration)
class ConfigurationAdmin(CompanyRestrictedAdmin,ModelAdmin):
    list_display = (
        "id",
        "name",
        "client",
        "subtotal_price",
        "total_price_display",
        "show_status_customized_color",
        "company"
    )

    list_filter = (("status",MultipleChoicesDropdownFilter), 
                   ("client",AutocompleteSelectMultipleFilter), 
                   ("created_at",RangeDateTimeFilter))
    search_fields = ("name", "client__name")
    list_select_related = ["client","company"]

    readonly_fields = ("total_price","total_cost","subtotal_price","company")
    actions_row = ["export_configuration_to_template", 
                   "convert_to_production"]
    autocomplete_fields = ["client","product"]

    inlines = [
        ParameterValueInline,
        ConfigurationItemInline,
        PricingRuleAssignmentInline,
        AppliedTaxInline
    ]

    @display(
        description=_("Status"),
        ordering="status",
        label={
            _(ConfigurationStatus.COMPLETED.label): "success",  # green
            _(ConfigurationStatus.DRAFT.label): "info",  # blue
            _(ConfigurationStatus.CONVERTED.label): "warning",  # orange
        },
    )
    def show_status_customized_color(self, obj):
        return _(obj.get_status_display())

    fieldsets = (
        (_("Configuration"), {
            "fields": (
                ("name", "client"),
                ("product", "status"),
                ("company")
            ),
        }),
        (_("Pricing"), {
            "fields": (
                ("total_cost","subtotal_price",),
                "total_price"
                ),
        }),
    )

    
    def get_urls(self):

        urls = super().get_urls()
        custom_urls = [
            path(
                "wizard/",
                self.admin_site.admin_view(ProductSelectionView.as_view(model_admin=self)),
                name="configurator_configuration_wizard_select",
            ),
            path(
                "wizard/<int:product_id>/",
                self.admin_site.admin_view(ConfigurationWizardView.as_view(model_admin=self)),
                name="configurator_configuration_wizard_form",
            ),
            path(
            "<int:config_id>/export/",
            self.admin_site.admin_view(
                ConfigurationExportView.as_view(model_admin=self)
            ),
            name="configurator_configuration_export",
        ),
        ]
        return custom_urls + urls

    def save_related(self, request, form, formsets, change):
        super().save_related(request, form, formsets, change)
        form.instance.generate_bom()
        form.instance.recalculate_price()

    def add_view(self, request, form_url="", extra_context=None):
        """
        Redirect the default 'Add' button to the wizard.
        """
        return redirect(
            reverse("admin:configurator_configuration_wizard_select")
        )

    @action(
        description=_("Export HTML"),
        url_path="configurationexportadmin-export-action",
        attrs={"target": "_blank"}
    )
    def export_configuration_to_template(self, request: HttpRequest, object_id: int):
        url = reverse("admin:configurator_configuration_export", args=[object_id])
        return redirect(url)
    
    @admin.display(description=_("Total price"))
    def total_price_display(self, obj):
        return obj.total_price
    
    @action(
    description=_("Convert to production"))
    def convert_to_production(self, request: HttpRequest, object_id: int):
        config = Configuration.objects.get(pk=object_id)
        flow = ConfigurationFlow(config)

        flow.convert()
        self.message_user(
                request,
                _("Configuration successfully converted to production."),
                "success")

        return redirect(
            reverse("admin:configurator_configuration_change", args=[object_id])
        )
