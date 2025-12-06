from django.contrib import admin
from unfold.admin import ModelAdmin
from configurator.models import ProductTemplate, ProductParameter, BOMRule, PricingRuleTemplate
from unfold.admin import TabularInline

class ProductParameterInline(TabularInline):
    model = ProductParameter
    extra = 0
    tab=True


class BOMRuleInline(TabularInline):
    model = BOMRule
    extra = 0
    fields = ("material", "quantity_expression", "length_expression")
    tab=True


class PricingRuleTemplateInline(TabularInline):
    model = PricingRuleTemplate
    extra = 0
    fields = ("name", "expression", "is_active")
    tab=True


@admin.register(ProductTemplate)
class ProductTemplateAdmin(ModelAdmin):
    list_display = ("id", "name")
    search_fields = ("name",)

    inlines = [
        ProductParameterInline,
        BOMRuleInline,
        PricingRuleTemplateInline,
    ]

    fieldsets = (
        ("Product Template", {
            "fields": ("name", "image","description"),
        }),
    )
