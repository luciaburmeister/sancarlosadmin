from django.contrib import admin
from unfold.admin import ModelAdmin
from materials.models import Category, Material, Provider,Component,ComponentPriceRule
from unfold.admin import TabularInline
from django_json_widget.widgets import JSONEditorWidget
from django.db import models
from django.utils.translation import gettext_lazy as _
from core.admin.company_admin import CompanyRestrictedAdmin
from unfold.contrib.forms.widgets import ArrayWidget
from django.contrib.postgres.fields import ArrayField
from django import forms

class ComponentPriceRuleInline(TabularInline):
    model = ComponentPriceRule
    extra = 0
    hide_ordering_field = True
    ordering_field = "priority"
    tab=True

    fields = (
        "condition",
        "unit_price_expression",
        "unit_price",
        "is_fallback",
    )

    verbose_name = _("Component price rule")
    verbose_name_plural = _("Component price rules")


@admin.register(Component)
class ComponentAdmin(CompanyRestrictedAdmin,ModelAdmin):
    list_display = (
        "name",
        "code",
        "component_type",
        "company"
    )

    list_filter = ("materials", "component_type",)

    list_select_related = ["company"]

    search_fields = (
        "name",
        "code",
        "materials__name",
        "materials__reference_code",
    )

    autocomplete_fields = ("materials","company")

    inlines = [ComponentPriceRuleInline]  # 👈 HERE

    formfield_overrides = {
        models.JSONField:{
            'widget': JSONEditorWidget,
        },
         ArrayField: {
            "widget": ArrayWidget,
        }
        }

    fieldsets = [
        (
            None,
            {
                "fields": (
                    "materials",
                    ("name",
                    "code"),
                    "component_type", 
                    ("unit_price_expression","cost_per_unit"),
                    ("company","production_stages")
                )
            },
        ),
        (
            "Metadata",
            {
                "fields": ("metadata",),
            },
        ),
    ]

    def get_form(self, request, obj=None, **kwargs):
        form = super().get_form(request, obj, **kwargs)
        if obj and obj.company and obj.company.production_stages:
            form.base_fields["production_stages"].widget = ArrayWidget(
                choices=[("", "---------")] + [(s, s) for s in obj.company.production_stages]
            )
        return form


@admin.register(Category)
class CategoryAdmin(ModelAdmin):
    list_display = ["name"]
    search_fields = ["name"]

@admin.register(Material)
class MaterialAdmin(CompanyRestrictedAdmin,ModelAdmin):
    list_display = [
        "reference_code", 
        "name", 
        "category", 
        "cost_per_unit", 
        "unit_type",
        "company"
    ]
    list_filter = ["category",  "unit_type"]
    search_fields = ["reference_code", "name"]
    ordering = ["reference_code"]
    list_filter = ["provider"]
    autocomplete_fields = ["provider","company"]
    list_select_related = ["company"]

    fieldsets = (
        ("Product Details", {
            "fields": (
                ("name"),
                ("reference_code", "rule_key"),
                ("category", "cost_per_unit"),
                ("unit_type","provider"),
                "company"
            )
        }),
    )


@admin.register(Provider)
class ProviderAdmin(CompanyRestrictedAdmin, ModelAdmin):
    list_display = ("name", "contact", "phone", "email", "tax_identifier","company")
    search_fields = ("name", "contact", "phone", "email", "tax_identifier", "associated_emails")
    list_select_related = ["company"]
    autocomplete_fields = ["company"]
    
    formfield_overrides = {
        ArrayField: {
            "widget": ArrayWidget,
        }
    }
    
    fieldsets = (
        (_("General Information"), {
            "fields": ("name","company")
        }),
        (_("Contact Details"), {
            "fields": ("contact", ("phone", "email"), "associated_emails")
        }),
        (_("Finance"), {
            "fields": ("tax_identifier",)
        }),
    )