from django.contrib import admin
from unfold.admin import ModelAdmin
from configurator.models import Configuration, ConfigurationItem, PricingRuleAssignment
from unfold.admin import TabularInline

class ConfigurationItemInline(TabularInline):
    model = ConfigurationItem
    extra = 1
    fields = ("material", "quantity", "unit_price", "line_total")
    readonly_fields = ("line_total",)


class PricingRuleAssignmentInline(TabularInline):
    model = PricingRuleAssignment
    extra = 1
    autocomplete_fields = ["rule"]



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

    readonly_fields = ("total_price", "created_at", "updated_at")

    inlines = [
        ConfigurationItemInline,
        PricingRuleAssignmentInline,
    ]

    fieldsets = (
        ("Configuration", {
            "fields": ("client", "name", "status"),
        }),
        ("Pricing", {
            "fields": ("total_price",),
        }),
    )

    def save_related(self, request, form, formsets, change):
        super().save_related(request, form, formsets, change)

        # Recalculate price after items + rules are saved
        form.instance.recalculate_price()
