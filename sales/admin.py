from django.contrib import admin
from unfold.admin import ModelAdmin,TabularInline
from .models import Client, Order
from production.models import ManufacturingItem
from unfold.widgets import UnfoldAdminColorInputWidget
from django.db import models

class ManufacturingItemInline(TabularInline):
    model = ManufacturingItem
    extra = 0
    tab = True
    fields = ["product_model", "width", "height", "quantity", "finish_ral", "observations"]
    formfield_overrides = {
        models.CharField: {"widget": UnfoldAdminColorInputWidget},
    }
@admin.register(Client)
class ClientAdmin(ModelAdmin):
    list_display = ["name", "phone", "email"]
    search_fields = ["name", "email", "phone", "address"]
    list_filter = []
    
    fieldsets = (
        ("Contact Information", {
            "fields": (
                "name",
                ("phone", "email"),
                "address"
            )
        }),
    )

@admin.register(Order)
class OrderAdmin(ModelAdmin):
    list_display = [
        "project_name", 
        "client", 
        "status", 
        "deadline_date", 
        "final_price_display"
    ]
    list_filter = ["status", "date_created", "deadline_date"]
    search_fields = ["project_name", "client__name"]
    autocomplete_fields = ["client"]
    readonly_fields = ["date_created", "final_price_display","total_cost_material","total_cost_labor"]
    inlines = [ManufacturingItemInline]
    fieldsets = (
        ("Project Details", {
            "fields": (
                ("project_name", "status"),
                "client",
                ("date_created", "deadline_date")
            )
        }),
        ("Financial Summary", {
            "classes": ("collapse",), 
            "description": "Calculated costs based on the manufacturing items associated with this order.",
            "fields": (
                ("total_cost_material", "total_cost_labor"),
                "final_price"
            )
        }),
    )

    def final_price_display(self, instance):
        return f"{instance.final_price:.2f} €"
    final_price_display.short_description = "Total Price"
    final_price_display.admin_order_field = "final_price"