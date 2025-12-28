from django.contrib import admin
from unfold.admin import ModelAdmin
from configurator.models import ProductTemplate, ProductParameter, BOMRule, PricingRuleTemplate, ProductComputedVariable
from unfold.admin import TabularInline,StackedInline
from django.db import models
from unfold.widgets import UnfoldAdminTextareaWidget
from unfold.contrib.forms.widgets import ArrayWidget
from django.contrib.postgres.fields import ArrayField


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
    hide_ordering_field = True
    ordering_field = "order"
    
    formfield_overrides = {
        models.JSONField: {"widget": UnfoldAdminTextareaWidget(attrs={"rows": 2})},
    }

class BOMRuleInline(StackedInline):
    model = BOMRule
    extra = 0
    fields = ("component", ("quantity_expression", "length_expression"),("production_stages","description"))
    tab=True
    autocomplete_fields = ["component"]

    formfield_overrides = {
        ArrayField: {
            "widget": ArrayWidget,
        }
    }

    def get_formset(self, request, obj=None, **kwargs):
        formset = super().get_formset(request, obj, **kwargs)

        if obj and obj.production_stages:
            formset.form.base_fields["production_stages"].widget = ArrayWidget(
                choices=[("", "---------")] + [(s, s) for s in obj.production_stages]
            )

        return formset

    

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        return qs.select_related("component","product")


class PricingRuleTemplateInline(TabularInline):
    model = PricingRuleTemplate
    extra = 0
    fields = ("name", "expression", "is_active")
    tab=True

    formfield_overrides = {
        models.TextField: {"widget": UnfoldAdminTextareaWidget(attrs={"rows": 2})},
    }


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
            "fields": (("name", "image"),"description","production_stages"),
        }),
    )

    formfield_overrides = {
        models.TextField: {"widget": UnfoldAdminTextareaWidget(attrs={"rows": 2})},
        ArrayField: {
            "widget": ArrayWidget,
        }
    }

    
