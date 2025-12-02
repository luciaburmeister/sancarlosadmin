from django.contrib import admin
from unfold.admin import ModelAdmin
from .models import Category, Material

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
        "cost_display", 
        "unit_type",
        "is_profile"
    ]
    list_filter = ["category", "is_profile", "unit_type"]
    search_fields = ["reference_code", "name"]
    ordering = ["reference_code"]

    fieldsets = (
        ("Product Details", {
            "fields": (
                ("reference_code", "name"),
                ("category", "cost_per_unit"),
                "unit_type"
            )
        }),
        ("Aluminum Profile Specs", {
            "classes": ("collapse",), 
            "description": "Configuration for raw bars (e.g., 6m lengths) that require optimization/cutting.",
            "fields": (
                "is_profile", 
                "bar_length"
            )
        }),
    )

    def cost_display(self, instance):
        return f"{instance.cost_per_unit:.2f} €"
    cost_display.short_description = "Cost"
    cost_display.admin_order_field = "cost_per_unit"