from django.contrib import admin
from django.utils.translation import gettext_lazy as _
from unfold.admin import ModelAdmin
from unfold.decorators import display
from .models import (
    Client, Order, DeliveryOrder, DeliveryOrderItem, DeliveryOrderStatus,
    Quote, QuoteItem, SaleZone, ClientAddress, ClientContact, ClientBankAccount
)
from unfold.admin import TabularInline, StackedInline
from configurator.models import Configuration
from configurator.admin.configuration import AppliedTaxInline
from django.shortcuts import redirect
from django.urls import reverse
from unfold.decorators import action
from core.admin.company_admin import CompanyRestrictedAdmin

@admin.register(SaleZone)
class SaleZoneAdmin(CompanyRestrictedAdmin, ModelAdmin):
    list_display = ["name", "company"]
    search_fields = ["name"]
    autocomplete_fields = ["company"]

@admin.register(ClientBankAccount)
class ClientBankAccountAdmin(ModelAdmin):
    list_display = ["bank_name", "account_number", "client"]
    search_fields = ["bank_name", "account_number", "iban"]
    autocomplete_fields = ["client"]

class ClientAddressInline(StackedInline):
    model = ClientAddress
    extra = 0
    tab = True
    tab_title = _("Addresses")

class ClientContactInline(StackedInline):
    model = ClientContact
    extra = 0
    tab = True
    tab_title = _("Contacts")
    # This will be linked to addresses, but for now we list them on the client
    # Alternatively, we could link them to the client directly if we want
    # but the model has them linked to Address. 
    # To show them on Client, I might need a through relation or just show them for all addresses.
    # Actually, I'll link them to Client directly in the model if that's easier for the UI.
    # But the requirement said "Persona de contacto 1 ... Dirección A".
    # I'll keep the model as is and add them to Client if I add a client FK.
    # Let's check the model I wrote for ClientContact. It has an address FK.

class ClientBankAccountInline(TabularInline):
    model = ClientBankAccount
    extra = 0
    tab = True
    tab_title = _("Bank Accounts")

@admin.register(Client)
class ClientAdmin(CompanyRestrictedAdmin, ModelAdmin):
    list_display = ["name", "tax_identifier", "phone", "email", "sale_zone", "agent"]
    search_fields = ["name", "tax_identifier", "email", "phone", "address"]
    list_filter = ["sale_zone", "agent", "is_insured"]
    autocomplete_fields = ["company", "agent", "sale_zone", "our_bank_account"]
    
    inlines = [ClientAddressInline, ClientContactInline, ClientBankAccountInline]

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        if db_field.name == "default_client_bank_account":
            # Get the object ID from the URL if we are editing an existing client
            import re
            match = re.search(r'/admin/sales/client/(\d+)/change/', request.path)
            if match:
                client_id = match.group(1)
                kwargs["queryset"] = ClientBankAccount.objects.filter(client_id=client_id)
            else:
                kwargs["queryset"] = ClientBankAccount.objects.none()
        return super().formfield_for_foreignkey(db_field, request, **kwargs)
    
    fieldsets = (
        (_("Basic Information"), {
            "classes": ["tab"],
            "fields": (
                ("name", "company"),
                ("tax_identifier", "phone", "email"),
                "address"
            )
            
        }),
        (_("Sales & Territory"), {
            "classes": ["tab"],
            "fields": (
                ("sale_zone", "agent"),
                "agent_commission",
            )
        }),
        (_("Financial & Insurance"), {
            "classes": ["tab"],
            "fields": (
                ("is_insured", "max_risk", "current_risk"),
                ("payment_method", "our_bank_account"),
                "default_client_bank_account",
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




class QuoteItemInline(TabularInline):
    model = QuoteItem
    extra = 1
    tab = True
    autocomplete_fields = ["material"]
    readonly_fields = ["line_total"]

class ConfigurationReadOnlyInline(TabularInline):
    model = Configuration
    extra = 0
    tab = True
    readonly_fields = ["name", "status", "subtotal_price", "total_price_display"]
    fields = ["name", "status", "subtotal_price", "total_price_display"]
    can_delete = True
    max_num = 0

    @display(description=_("Total price"))
    def total_price_display(self, obj):
        return obj.total_price

@admin.register(Quote)
class QuoteAdmin(CompanyRestrictedAdmin, ModelAdmin):
    list_display = ["id", "client", "project_name", "status", "total_price_display", "company"]
    list_filter = ["status", "company", "created_at"]
    search_fields = ["project_name", "client__name", "id"]
    autocomplete_fields = ["client"]
    readonly_fields = ["subtotal_price", "tax_amount", "total_price", "tax_breakdown", "company"]
    
    inlines = [ConfigurationReadOnlyInline, QuoteItemInline, AppliedTaxInline]

    actions_row = ["add_configuration_to_quote"]

    fieldsets = (
        ("Quote Details", {
            "fields": (
                ("client", "status"),
                ("project_name", "company"),
                "expires_at"
            )
        }),
        ("Financials", {
            "fields": (
                ("subtotal_price", "tax_amount"),
                "total_price",
                "tax_breakdown"
            )
        }),
    )

    @display(description=_("Total price"))
    def total_price_display(self, obj):
        return obj.total_price

    @action(description=_("Add Configuration"))
    def add_configuration_to_quote(self, request, object_id: int):
        url = reverse("admin:configurator_configuration_wizard_select")
        return redirect(f"{url}?quote_id={object_id}")

    def save_related(self, request, form, formsets, change):
        super().save_related(request, form, formsets, change)
        form.instance.recalc_totals()