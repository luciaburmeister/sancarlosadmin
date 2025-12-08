from django.contrib import admin
from unfold.admin import ModelAdmin
from configurator.models import Configuration, ConfigurationParameterValue, ConfigurationItem, PricingRuleAssignment
from unfold.admin import TabularInline
from django.urls import path
from configurator.views import ProductSelectionView, ConfigurationWizardView

class ParameterValueInline(TabularInline):
    model = ConfigurationParameterValue
    extra = 0
    fields = ("parameter", "value")
    tab=True
    autocomplete_fields = ["parameter"]
class ConfigurationItemInline(TabularInline):
    model = ConfigurationItem
    extra = 0
    fields = ("is_template_item","material", "quantity", "unit_price", "line_total")
    readonly_fields = ("line_total","is_template_item")
    tab=True
    autocomplete_fields = ["material"]

class PricingRuleAssignmentInline(TabularInline):
    model = PricingRuleAssignment
    extra = 0
    autocomplete_fields = ["rule"]
    tab=True



@admin.register(Configuration)
class ConfigurationAdmin(ModelAdmin):
    list_display = (
        "id",
        "name",
        "client",
        "status",
        "total_price",
        "created_at",
        "updated_at",
    )

    list_filter = ("status", "client", "created_at")
    search_fields = ("name", "client__name")

    readonly_fields = ("total_price",)

    autocomplete_fields = ["client","product"]

    inlines = [
        ParameterValueInline,
        ConfigurationItemInline,
        PricingRuleAssignmentInline,
    ]

    fieldsets = (
        ("Configuration", {
            "fields": (("name","client"), 
                       ("product","status")),
        }),
        ("Pricing", {
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
        ]
        return custom_urls + urls

    def save_related(self, request, form, formsets, change):
        super().save_related(request, form, formsets, change)
        form.instance.generate_bom()
        form.instance.recalculate_price()