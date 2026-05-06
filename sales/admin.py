from django.contrib import admin
from django.utils.translation import gettext_lazy as _
from unfold.admin import ModelAdmin
from unfold.decorators import display
from .models import (
    Client, Order, DeliveryOrder, DeliveryOrderItem, DeliveryOrderStatus,
    Quote, QuoteItem, SaleZone, ClientAddress, ClientContact, ClientBankAccount, QuoteStatus
)
from unfold.admin import TabularInline, StackedInline
from configurator.models import Configuration
from configurator.admin.configuration import AppliedTaxInline
from django.shortcuts import redirect, render
from django.urls import reverse, path
from unfold.decorators import action
from core.admin.company_admin import CompanyRestrictedAdmin
from management.views import QuoteAcceptanceWizardView
from .views.quote_export_view import QuoteExportView
from .views.quote_dashboard_view import QuoteDashboardView
from .components.quote_dashboard_charts import QuoteValueByStatusChart, QuoteCountByStatusChart  # noqa: F401

@admin.register(SaleZone)
class SaleZoneAdmin(CompanyRestrictedAdmin, ModelAdmin):
    list_display = ["name", "company"]
    search_fields = ["name"]
    autocomplete_fields = ["company"]

@admin.register(ClientBankAccount)
class ClientBankAccountAdmin(CompanyRestrictedAdmin,ModelAdmin):
    list_display = ["bank_name", "account_number", "client"]
    search_fields = ["bank_name", "account_number", "iban"]
    autocomplete_fields = ["client"]

@admin.register(ClientAddress)
class ClientAddressAdmin(CompanyRestrictedAdmin,ModelAdmin):
    list_display = ["street", "city", "province", "country", "client"]
    search_fields = ["street", "city", "postal_code", "province", "country", "client__name"]
    autocomplete_fields = ["client"]

class ClientAddressInline(StackedInline):
    model = ClientAddress
    extra = 0
    tab = True
    tab_title = _("Addresses")
    fieldsets = (
        (None, {
            "fields": (
                "address_type",
                "country",
                ("province", "city", "postal_code"),
                "street",
            ),
        }),
    )

class ClientContactInline(StackedInline):
    model = ClientContact
    extra = 0
    tab = True
    tab_title = _("Contacts")
    autocomplete_fields = ["address"]
    fieldsets = (
        (None, {
            "fields": (
                ("name", "position"),
                ("email", "phone"),
                "address",
            ),
        }),
    )

class ClientBankAccountInline(StackedInline):
    model = ClientBankAccount
    extra = 0
    tab = True
    tab_title = _("Bank Accounts")
    fieldsets = (
        (None, {
            "fields": (
                ("bank_name", "holder_name"),
                ("iban", "swift_bic"),
                ("account_number", "routing_number"),
                ("country", "is_default"),
            ),
        }),
    )

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
class DeliveryOrderAdmin(CompanyRestrictedAdmin, ModelAdmin):
    list_display = ["number", "incremental_id", "get_status_label", "company", "client", "date", "sales_invoice"]
    list_filter = ["company", "status", "date"]
    search_fields = ["number", "client__name"]
    autocomplete_fields = ["company", "client", "sales_invoice", "configuration", "project_file"]
    inlines = [DeliveryOrderItemInline]
    readonly_fields = ["number", "date"]

    @display(
        description=_("Status"),
        ordering="status",
        label={
            _(DeliveryOrderStatus.DRAFT): "warning",  
            _(DeliveryOrderStatus.DELIVERED): "success",  
            _(DeliveryOrderStatus.SENT): "info",  
            _(DeliveryOrderStatus.CANCELLED): "danger",  
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
    list_display = ["id", "client", "project_name","project_file", "show_status_customized_color", "total_price_display", "company"]
    list_filter = ["status", "company", "created_at"]
    search_fields = ["project_name", "client__name", "id"]
    autocomplete_fields = ["client", "project_file","company"]
    readonly_fields = ["subtotal_price", "tax_amount", "total_price", "tax_breakdown"]
    list_select_related = ["client", "project_file", "company"]
    
    inlines = [ConfigurationReadOnlyInline, QuoteItemInline, AppliedTaxInline]

    actions_list = ["quote_dashboard_action"]
    actions_row = ["add_configuration_to_quote", "export_quote_action"]

    fieldsets = (
        ("Quote Details", {
            "fields": (
                ("client", "status"),
                ("project_name", "company"),
                "project_file",
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

    @display(
        description=_("Status"),
        ordering="status",
        label={
            _(QuoteStatus.ACCEPTED.label): "success",  # green
            _(QuoteStatus.SENT.label): "info",  # blue
            _(QuoteStatus.REJECTED.label): "warning",  # orange
            _(QuoteStatus.DRAFT.label): "primary",  # orange
            
        },
    )
    def show_status_customized_color(self, obj):
        return _(obj.get_status_display())

    @action(description=_("Add Configuration"))
    def add_configuration_to_quote(self, request, object_id: int):
        url = reverse("admin:configurator_configuration_wizard_select")
        return redirect(f"{url}?quote_id={object_id}")

    def save_related(self, request, form, formsets, change):
        super().save_related(request, form, formsets, change)
        form.instance.recalc_totals()

    @action(description=_("Export Quote"), attrs={"target": "_blank"})
    def export_quote_action(self, request, object_id: int):
        return redirect(reverse("admin:sales_quote_export", args=[object_id]))

    @action(
        description=_("Quote Dashboard"),
        url_path="quote-dashboard-action",
        permissions=["quote_dashboard"],
        icon="bar_chart",
    )
    def quote_dashboard_action(self, request):
        return redirect(reverse("admin:sales_quote_dashboard"))

    def has_quote_dashboard_permission(self, request):
        return request.user.has_perm("sales.can_view_quote_dashboard")

    def get_urls(self):
        urls = super().get_urls()
        custom_urls = [
            path(
                "dashboard/",
                self.admin_site.admin_view(
                    QuoteDashboardView.as_view(model_admin=self)
                ),
                name="sales_quote_dashboard",
            ),
            path(
                "<int:quote_id>/accept-wizard/",
                self.admin_site.admin_view(QuoteAcceptanceWizardView.as_view(model_admin=self)),
                name="sales_quote_acceptance_wizard",
            ),
            path(
                "<int:quote_id>/export/",
                self.admin_site.admin_view(QuoteExportView.as_view(model_admin=self)),
                name="sales_quote_export",
            ),
        ]
        return custom_urls + urls

    def response_change(self, request, obj):
        if obj.status == "accepted" and not obj.project_file:
            return redirect("admin:sales_quote_acceptance_wizard", quote_id=obj.pk)
        return super().response_change(request, obj)

    def response_add(self, request, obj, post_url_continue=None):
        if obj.status == "accepted" and not obj.project_file:
            return redirect("admin:sales_quote_acceptance_wizard", quote_id=obj.pk)
        return super().response_add(request, obj, post_url_continue)