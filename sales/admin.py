from django.contrib import admin
from django.utils.translation import gettext_lazy as _
from unfold.admin import ModelAdmin
from unfold.decorators import display
from .models import Client, Order, DeliveryOrder, DeliveryOrderItem, DeliveryOrderStatus
from unfold.admin import TabularInline

from core.admin.company_admin import CompanyRestrictedAdmin

@admin.register(Client)
class ClientAdmin(CompanyRestrictedAdmin, ModelAdmin):
    list_display = ["name", "phone", "email"]
    search_fields = ["name", "email", "phone", "address"]
    list_filter = []
    autocomplete_fields = ["company"]
    
    fieldsets = (
        ("Contact Information", {
            "fields": (
                ("name","company"),
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


class DeliveryOrderItemInline(TabularInline):
    model = DeliveryOrderItem
    extra = 0
    tab = True

@admin.register(DeliveryOrder)
class DeliveryOrderAdmin(ModelAdmin):
    list_display = ["number", "incremental_id", "get_status_label", "company", "client", "date", "sales_invoice"]
    list_filter = ["company", "status", "date"]
    search_fields = ["number", "client__name"]
    autocomplete_fields = ["company", "client", "sales_invoice", "configuration"]
    inlines = [DeliveryOrderItemInline]
    readonly_fields = ["number", "date"]

    @display(
        description=_("Status"),
        ordering="status",
        label={
            _(DeliveryOrderStatus.DRAFT): "warning",  # green
            _(DeliveryOrderStatus.DELIVERED): "success",  # green
            _(DeliveryOrderStatus.SENT): "info",  # blue
            _(DeliveryOrderStatus.CANCELLED): "danger",  # orange
        },
    )
    def get_status_label(self, obj):
        return obj.status