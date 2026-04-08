from django.contrib import admin
from django.utils.translation import gettext_lazy as _
from unfold.admin import ModelAdmin, TabularInline, StackedInline
from .models import ProjectFile, ProjectFileStatus
from sales.models import Quote, DeliveryOrder
from production.models import ProductionTicket
from invoices.models import SalesInvoice, Invoice
from core.admin.company_admin import CompanyRestrictedAdmin

class QuoteInline(TabularInline):
    model = Quote
    extra = 0
    tab = True
    tab_title = _("Quotes")
    fields = ["id", "project_name", "status", "total_price"]
    readonly_fields = ["id", "project_name", "status", "total_price"]
    show_change_link = True

class SalesInvoiceInline(TabularInline):
    model = SalesInvoice
    extra = 0
    tab = True
    tab_title = _("Sales Invoices")
    fields = ["invoice_number", "date_issued", "total_amount", "status"]
    readonly_fields = ["invoice_number", "date_issued", "total_amount", "status"]
    show_change_link = True

class ProductionTicketInline(TabularInline):
    model = ProductionTicket
    extra = 0
    tab = True
    tab_title = _("Production")
    fields = ["pk", "status", "created_at"]
    readonly_fields = ["pk", "status", "created_at"]
    show_change_link = True

class DeliveryOrderInline(TabularInline):
    model = DeliveryOrder
    extra = 0
    tab = True
    tab_title = _("Logistics")
    fields = ["number", "date", "status"]
    readonly_fields = ["number", "date", "status"]
    show_change_link = True

class PurchaseInvoiceInline(TabularInline):
    model = Invoice
    extra = 0
    tab = True
    tab_title = _("Expenses")
    fields = ["invoice_number", "provider", "total_amount", "date_issued"]
    readonly_fields = ["invoice_number", "provider", "total_amount", "date_issued"]
    show_change_link = True

@admin.register(ProjectFile)
class ProjectFileAdmin(CompanyRestrictedAdmin, ModelAdmin):
    list_display = ["reference", "name", "client", "status", "start_date", "company"]
    list_filter = ["status", "company", "created_at"]
    search_fields = ["reference", "name", "client__name"]
    autocomplete_fields = ["client", "company"]
    
    readonly_fields = ["reference", "incremental_id"]

    inlines = [
        QuoteInline,
        SalesInvoiceInline,
        ProductionTicketInline,
        DeliveryOrderInline,
        PurchaseInvoiceInline
    ]

    fieldsets = (
        (_("Basic Information"), {
            "fields": (
                ("reference", "company"),
                ("name", "client"),
                "status",
                "description",
            )
        }),
        (_("Timeline"), {
            "fields": (
                ("start_date", "end_date"),
            )
        }),
        (_("Internal Data"), {
            "classes": ("collapse",),
            "fields": (
                "incremental_id",
            )
        }),
    )
