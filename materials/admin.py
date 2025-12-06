from django.contrib import admin
from unfold.admin import ModelAdmin
from materials.models import Category, Material, Provider

@admin.register(Category)
class CategoryAdmin(ModelAdmin):
    list_display = ["name"]
    search_fields = ["name"]

@admin.register(Material)
class MaterialAdmin(ModelAdmin):
    list_display = [
        "reference_code", 
        "name", 
        "category", 
        "cost_per_unit", 
        "unit_type",
    ]
    list_filter = ["category",  "unit_type"]
    search_fields = ["reference_code", "name"]
    ordering = ["reference_code"]
    list_filter = ["provider"]
    autocomplete_fields = ["provider"]

    fieldsets = (
        ("Product Details", {
            "fields": (
                ("reference_code", "name"),
                ("category", "cost_per_unit"),
                ("unit_type","provider")
            )
        }),
    )

@admin.register(Provider)
class ProviderAdmin(ModelAdmin):
    list_display = ("name", "contact", "phone", "email", "tax_identifier")
    search_fields = ("name", "contact", "phone", "email", "tax_identifier")

    fieldsets = (
        ("General Information", {
            "fields": ("name",)
        }),
        ("Contact Details", {
            "fields": ("contact", ("phone", "email"))
        }),
        ("Finance", {
            "fields": ("tax_identifier",)
        }),
    )