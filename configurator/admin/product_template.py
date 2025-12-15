from django.contrib import admin
from unfold.admin import ModelAdmin
from configurator.models import ProductTemplate, ProductParameter, BOMRule, PricingRuleTemplate, ProductComputedVariable
from unfold.admin import TabularInline
from django.db import models
from unfold.widgets import UnfoldAdminTextareaWidget

class ProductComputedVariableInline(TabularInline):
    model = ProductComputedVariable
    extra = 0
    fields = ("name", "expression")
    verbose_name = "Computed Variable"
    verbose_name_plural = "Computed Variables"
    tab = True


class ProductParameterInline(TabularInline):
    model = ProductParameter
    extra = 0
    tab=True


class BOMRuleInline(TabularInline):
    model = BOMRule
    extra = 0
    fields = ("component", "quantity_expression", "length_expression")
    tab=True
    autocomplete_fields = ["component"]

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        return qs.select_related("component")


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
        ProductComputedVariableInline,
        BOMRuleInline,
        PricingRuleTemplateInline,
    ]

    fieldsets = (
        ("Product Template", {
            "fields": ("name", "image","description"),
        }),
    )

    formfield_overrides = {
        models.TextField: {"widget": UnfoldAdminTextareaWidget(attrs={"rows": 2})},
    }

    
