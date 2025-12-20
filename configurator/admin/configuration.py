from django.contrib import admin
from unfold.admin import ModelAdmin
from configurator.models import Configuration, ConfigurationParameterValue, ConfigurationItem, PricingRuleAssignment
from unfold.admin import TabularInline
from django.urls import path
from configurator.views import ProductSelectionView, ConfigurationWizardView, ConfigurationExportView
from django.urls import reverse
from django.shortcuts import redirect
from unfold.decorators import action
from django.utils.translation import gettext_lazy as _
from django.http import HttpRequest

class ParameterValueInline(TabularInline):
    model = ConfigurationParameterValue
    extra = 0
    fields = ("parameter", "value")
    tab = True
    autocomplete_fields = ["parameter"]
    verbose_name = _("Parameter value")
    verbose_name_plural = _("Parameter values")
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

class PricingRuleAssignmentInline(TabularInline):
    model = PricingRuleAssignment
    extra = 0
    autocomplete_fields = ["rule"]
    tab = True
    verbose_name = _("Pricing rule assignment")
    verbose_name_plural = _("Pricing rule assignments")



@admin.register(Configuration)
class ConfigurationAdmin(ModelAdmin):
    list_display = (
        "id",
        "name",
        "client",
        "status",
        "total_price_display",
        "created_at",
    )

    list_filter = ("status", "client", "created_at")
    search_fields = ("name", "client__name")

    readonly_fields = ("total_price",)
    actions_row = ["export_configuration_to_template"]
    autocomplete_fields = ["client","product"]

    inlines = [
        ParameterValueInline,
        ConfigurationItemInline,
        PricingRuleAssignmentInline,
    ]

    fieldsets = (
        (_("Configuration"), {
            "fields": (
                ("name", "client"),
                ("product", "status"),
            ),
        }),
        (_("Pricing"), {
            "fields": ("total_price",),
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