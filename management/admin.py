from django.contrib import admin
from django.shortcuts import redirect
from django.urls import path, reverse
from django.utils.translation import gettext_lazy as _
from django.utils.html import format_html
from unfold.admin import ModelAdmin, TabularInline, StackedInline
from unfold.decorators import action, display
from .models import ProjectFile, ProjectFileStatus
from sales.models import Quote, QuoteStatus, DeliveryOrder
from production.models import ProductionTicket
from invoices.models import SalesInvoice, Invoice
from invoices.views.invoice.project_file_wizard_views import ProjectFileInvoiceWizardView
from invoices.services.project_file_invoice_service import ProjectFileInvoiceService
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
    list_display = ["reference", "name", "client", "status", "start_date", "company", "invoicing_status"]
    list_filter = ["status", "company", "created_at"]
    search_fields = ["reference", "name", "client__name"]
    autocomplete_fields = ["client", "company"]
    
    readonly_fields = ["reference", "incremental_id", "invoicing_summary_display"]

    inlines = [
        QuoteInline,
        SalesInvoiceInline,
        ProductionTicketInline,
        DeliveryOrderInline,
        PurchaseInvoiceInline
    ]

    actions_row = ["create_invoice_row_action"]
    actions_detail = ["create_invoice_detail_action"]

    fieldsets = (
        (_("Basic Information"), {
            "fields": (
                ("reference", "company"),
                ("name", "client"),
                "status",
                "description",
            )
        }),
        (_("Invoicing"), {
            "fields": (
                "invoicing_summary_display",
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

    def get_urls(self):
        urls = super().get_urls()
        custom = [
            path(
                "<int:project_file_id>/create-invoice/",
                self.admin_site.admin_view(
                    ProjectFileInvoiceWizardView.as_view(model_admin=self)
                ),
                name="management_projectfile_create_invoice",
            ),
        ]
        return custom + urls

    @action(description=_("Create Invoice"), icon="receipt_long")
    def create_invoice_row_action(self, request, object_id):
        return redirect(
            reverse("admin:management_projectfile_create_invoice", args=[object_id])
        )

    @action(description=_("Create Invoice"), icon="receipt_long")
    def create_invoice_detail_action(self, request, object_id):
        return redirect(
            reverse("admin:management_projectfile_create_invoice", args=[object_id])
        )

    @display(description=_("Invoicing"))
    def invoicing_status(self, obj):
        """List display column showing invoicing progress."""
        has_accepted = Quote.objects.filter(
            project_file=obj,
            status=QuoteStatus.ACCEPTED,
        ).exists()
        if not has_accepted:
            return "—"

        summary = ProjectFileInvoiceService.get_invoiceable_summary(obj)
        pct = summary['percentage_invoiced']
        if pct >= 100:
            return format_html(
                '<span style="color: #16a34a; font-weight: 600;">✓ {}%</span>',
                pct
            )
        elif pct > 0:
            return format_html(
                '<span style="color: #2563eb; font-weight: 600;">{}%</span>',
                pct
            )
        return format_html(
            '<span style="color: #9ca3af;">0%</span>'
        )

    @admin.display(description=_("Invoicing Summary"))
    def invoicing_summary_display(self, obj):
        """Detail page read-only field showing full invoicing breakdown."""
        if not obj.pk:
            return "—"

        has_accepted = Quote.objects.filter(
            project_file=obj,
            status=QuoteStatus.ACCEPTED,
        ).exists()
        if not has_accepted:
            return format_html(
                '<span style="color: #6b7280;">{}</span>',
                _("No accepted quotes yet.")
            )

        summary = ProjectFileInvoiceService.get_invoiceable_summary(obj)

        html = f"""
        <div style="display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px; max-width: 600px;">
            <div style="background: #f9fafb; border-radius: 8px; padding: 12px; text-align: center;">
                <div style="font-size: 11px; color: #6b7280; margin-bottom: 4px;">{_("Total Invoiceable")}</div>
                <div style="font-size: 18px; font-weight: 700; color: #111827;">{summary['total_invoiceable']:.2f} €</div>
            </div>
            <div style="background: #eff6ff; border-radius: 8px; padding: 12px; text-align: center;">
                <div style="font-size: 11px; color: #2563eb; margin-bottom: 4px;">{_("Already Invoiced")}</div>
                <div style="font-size: 18px; font-weight: 700; color: #1d4ed8;">{summary['total_invoiced']:.2f} €</div>
            </div>
            <div style="background: #f0fdf4; border-radius: 8px; padding: 12px; text-align: center;">
                <div style="font-size: 11px; color: #16a34a; margin-bottom: 4px;">{_("Remaining")}</div>
                <div style="font-size: 18px; font-weight: 700; color: #15803d;">{summary['remaining']:.2f} €</div>
            </div>
        </div>
        <div style="margin-top: 8px;">
            <div style="background: #e5e7eb; border-radius: 999px; height: 8px; overflow: hidden;">
                <div style="background: #2563eb; height: 100%; width: {summary['percentage_invoiced']}%; border-radius: 999px;"></div>
            </div>
            <div style="font-size: 11px; color: #6b7280; margin-top: 4px;">{summary['percentage_invoiced']}% {_("invoiced")}</div>
        </div>
        """
        return format_html(html)
