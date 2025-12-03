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
    ]
    list_filter = ["category",  "unit_type"]
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
    )

    def cost_display(self, instance):
        return f"{instance.cost_per_unit:.2f} €"
    cost_display.short_description = "Cost"
    cost_display.admin_order_field = "cost_per_unit"