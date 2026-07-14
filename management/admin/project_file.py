from unfold.admin import GenericStackedInline
from unfold.contrib.filters.admin import RangeDateFilter
from unfold.contrib.filters.admin.dropdown_filters import MultipleChoicesDropdownFilter

from decimal import Decimal
from management.models import ProjectFile, ResourcePlanning, ResourcePlanningBacklogItem
from management.resources import ProjectFileResource
from django.contrib import admin, messages
from django.shortcuts import redirect
from django.urls import path, reverse
from django.utils.translation import gettext_lazy as _
from django.utils.html import format_html
from unfold.admin import ModelAdmin, TabularInline
from unfold.admin import GenericTabularInline
from unfold.decorators import action, display
from import_export.admin import ExportActionModelAdmin
from unfold.contrib.import_export.forms import SelectableFieldsExportForm
from sales.models import Quote, QuoteStatus, DeliveryOrder
from production.models import ProductionTicket
from invoices.models import SalesInvoice, Invoice, AppliedTax
from invoices.views.invoice.project_file_wizard_views import ProjectFileInvoiceWizardView
from invoices.views.invoice.delivery_order_wizard_views import DeliveryOrderWizardView
from invoices.views.invoice.bulk_invoice_views import BulkInvoiceConfirmView
from invoices.services.project_file_invoice_service import ProjectFileInvoiceService
from core.admin.company_admin import CompanyRestrictedAdmin
from configurator.models import Configuration, ConfigurationStatus
from management.models import WorkEntry
from unfold.contrib.filters.admin import (
    AutocompleteSelectMultipleFilter
)


class QuoteInline(TabularInline):
    model = Quote
    extra = 0
    tab = True
    tab_title = _("Quotes")
    fields = ["id", "project_name", "status", "discounted_subtotal", "total_price"]
    readonly_fields = ["id", "project_name", "status", "discounted_subtotal", "total_price"]
    show_change_link = True

class SalesInvoiceInline(TabularInline):
    model = SalesInvoice
    extra = 0
    tab = True
    tab_title = _("Sales Invoices")
    fields = ["invoice_number", "date_issued", "total_amount", "status"]
    readonly_fields = ["invoice_number", "date_issued", "total_amount", "status"]
    show_change_link = True

class BulkSalesInvoiceInline(TabularInline):
    model = SalesInvoice.project_files.through
    extra = 0
    tab = True
    tab_title = _("Sales Invoices")  # same tab as single-PF invoices
    verbose_name = _("Bulk Invoice")
    verbose_name_plural = _("Bulk Invoices")
    fields = ["invoice_number", "date_issued", "pf_amount", "total_amount", "invoice_status"]
    readonly_fields = ["invoice_number", "date_issued", "pf_amount", "total_amount", "invoice_status"]
    show_change_link = False

    @display(description=_("Invoice Number"))
    def invoice_number(self, obj):
        inv = obj.salesinvoice
        label = inv.invoice_number or f"Pro Forma #{inv.id}"
        url = reverse("admin:invoices_salesinvoice_change", args=[inv.pk])
        return format_html('<a href="{}">{}</a>', url, label)

    @display(description=_("Date Issued"))
    def date_issued(self, obj):
        return obj.salesinvoice.date_issued

    @display(description=_("This PF"))
    def pf_amount(self, obj):
        from django.db.models import Sum
        total = obj.salesinvoice.items.filter(
            project_file_id=obj.projectfile_id,
        ).aggregate(s=Sum("line_total"))["s"]
        if total and hasattr(total, "amount"):
            total = total.amount
        return f"{total or 0:.2f} €"

    @display(description=_("Invoice Total"))
    def total_amount(self, obj):
        return obj.salesinvoice.total_amount

    @display(description=_("Status"))
    def invoice_status(self, obj):
        return obj.salesinvoice.get_status_display()

    def has_add_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

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

class DirectConfigurationInline(TabularInline):
    model = Configuration
    fk_name = "project_file"
    extra = 0
    tab = True
    tab_title = _("Direct Work")
    fields = ["name", "product", "status", "subtotal_price", "total_price"]
    readonly_fields = ["name", "product", "status", "subtotal_price", "total_price"]
    show_change_link = True

    def get_queryset(self, request):
        return super().get_queryset(request).filter(quote__isnull=True)

class ResourcePlanningInline(TabularInline):
    model = ResourcePlanning
    extra = 0
    tab = True
    tab_title = _("Resource Planning")
    fields = ["planning_type", "resource_content_type", "resource_object_id", "start", "end", "status", "notes"]
    readonly_fields = []
    autocomplete_fields = ["planning_type","resource_content_type"]


class ResourcePlanningBacklogInline(TabularInline):
    model = ResourcePlanningBacklogItem
    extra = 0
    tab = True
    tab_title = _("Planning Backlog")
    fields = ["planning_type", "estimated_days", "priority", "is_reusable", "status", "notes"]
    readonly_fields = ["status"]
    autocomplete_fields = ["planning_type"]

    def get_queryset(self, request):
        return super().get_queryset(request).select_related("planning_type")


class WorkEntryAppliedTaxInline(GenericStackedInline):
    model = AppliedTax
    extra = 0
    collapsible = True
    autocomplete_fields = ("tax_type",)
    fieldsets = (
        (_("Tax"), {
            "fields": ("tax_type", "selected_percentage"),
        }),
        (_("Computed"), {
            "fields": ("base_amount", "calculated_amount"),
        }),
    )
    readonly_fields = ("tax_type", "selected_percentage", "base_amount", "calculated_amount")
    verbose_name = _("Applied Tax")
    verbose_name_plural = _("Applied Taxes")

    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


class WorkEntryInline(TabularInline):
    model = WorkEntry
    extra = 0
    tab = True
    tab_title = _("Work Entries")
    fields = ["entry_type", "description", "quantity", "unit_price", "total_price", "total_with_tax", "date"]
    readonly_fields = ["total_price", "total_with_tax"]
    autocomplete_fields = ["entry_type"]
    inlines = [WorkEntryAppliedTaxInline]

    def get_queryset(self, request):
        return super().get_queryset(request).select_related("entry_type")


@admin.register(ProjectFile)
class ProjectFileAdmin(CompanyRestrictedAdmin, ModelAdmin, ExportActionModelAdmin):
    resource_classes = [ProjectFileResource]
    export_form_class = SelectableFieldsExportForm
    list_display = ["reference", "name", "client", "status", "total_amount", "total_invoiced", "start_date", "invoicing_status", "company"]
    list_filter = [("client", AutocompleteSelectMultipleFilter), ("status", MultipleChoicesDropdownFilter), ("company", AutocompleteSelectMultipleFilter), ("created_at", RangeDateFilter)]
    search_fields = ["reference", "name", "client__name"]
    autocomplete_fields = ["client", "company"]
    list_select_related = ["client","company"]
    list_filter_submit = True
    readonly_fields = ["reference", "incremental_id", "invoicing_summary_display"]

    inlines = [
        QuoteInline,
        DirectConfigurationInline,
        WorkEntryInline,
        SalesInvoiceInline,
        BulkSalesInvoiceInline,
        ProductionTicketInline,
        DeliveryOrderInline,
        PurchaseInvoiceInline,
        ResourcePlanningInline,
        ResourcePlanningBacklogInline,
    ]

    actions_row = ["create_invoice_row_action", "create_delivery_order_row_action"]
    actions_detail = ["create_invoice_detail_action", "create_delivery_order_detail_action"]
    actions = ["create_bulk_invoice"]

    fieldsets = (
        (_("Basic Information"), {
            "fields": (
                ("reference", "company"),
                ("name", "client"),
                "status",
                "description",
                "notification_email",
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

    def get_queryset(self, request):
        return super().get_queryset(request)

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
            path(
                "<int:project_file_id>/create-delivery-order/",
                self.admin_site.admin_view(
                    DeliveryOrderWizardView.as_view(model_admin=self)
                ),
                name="management_projectfile_create_delivery_order",
            ),
            path(
                "bulk-invoice/",
                self.admin_site.admin_view(
                    BulkInvoiceConfirmView.as_view(model_admin=self)
                ),
                name="management_projectfile_bulk_invoice",
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

    @action(description=_("Create Delivery Order"), icon="local_shipping")
    def create_delivery_order_row_action(self, request, object_id):
        return redirect(
            reverse("admin:management_projectfile_create_delivery_order", args=[object_id])
        )

    @action(description=_("Create Delivery Order"), icon="local_shipping")
    def create_delivery_order_detail_action(self, request, object_id):
        return redirect(
            reverse("admin:management_projectfile_create_delivery_order", args=[object_id])
        )

    @action(description=_("Create Bulk Invoice"), icon="receipt_long")
    def create_bulk_invoice(self, request, queryset):
        pf_list = list(queryset)
        if len(pf_list) < 2:
            messages.warning(request, _("Select at least 2 project files for bulk invoicing."))
            return None

        client_ids = {pf.client_id for pf in pf_list}
        if len(client_ids) > 1:
            messages.error(request, _("All selected project files must belong to the same client."))
            return None

        request.session["bulk_invoice_pf_ids"] = [pf.pk for pf in pf_list]
        return redirect(reverse("admin:management_projectfile_bulk_invoice"))

    @display(description=_("Invoicing"))
    def invoicing_status(self, obj):
        """List display column — reads from cached model fields."""
        pct = obj.percentage_invoiced or Decimal("0")
        if obj.total_amount.amount <= 0:
            return "—"

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
        """Detail page read-only field showing full invoicing breakdown (subtotal-based)."""
        if not obj.pk:
            return "—"

        summary = ProjectFileInvoiceService.get_invoiceable_summary(obj)
        if summary['total_invoiceable_subtotal_net'] <= 0:
            return format_html(
                '<span style="color: #6b7280;">{}</span>',
                _("No accepted quotes or direct configurations yet.")
            )

        # Net-of-discount figures: comparable to invoice subtotals regardless
        # of whether the discount was applied per-line or invoice-level.
        invoiceable = summary['total_invoiceable_subtotal_net']
        invoiced = summary['total_invoiced_subtotal_net']
        remaining = summary['remaining_subtotal_net']
        pct = (
            (invoiced / invoiceable * Decimal("100")).quantize(Decimal("0.01"))
            if invoiceable > 0 else Decimal("0.00")
        )

        html = f"""
        <div style="display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px; max-width: 600px;">
            <div style="background: #f9fafb; border-radius: 8px; padding: 12px; text-align: center;">
                <div style="font-size: 11px; color: #6b7280; margin-bottom: 4px;">{_("Total Invoiceable (subtotal)")}</div>
                <div style="font-size: 18px; font-weight: 700; color: #111827;">{invoiceable:.2f} €</div>
            </div>
            <div style="background: #eff6ff; border-radius: 8px; padding: 12px; text-align: center;">
                <div style="font-size: 11px; color: #2563eb; margin-bottom: 4px;">{_("Already Invoiced (subtotal)")}</div>
                <div style="font-size: 18px; font-weight: 700; color: #1d4ed8;">{invoiced:.2f} €</div>
            </div>
            <div style="background: #f0fdf4; border-radius: 8px; padding: 12px; text-align: center;">
                <div style="font-size: 11px; color: #16a34a; margin-bottom: 4px;">{_("Remaining (subtotal)")}</div>
                <div style="font-size: 18px; font-weight: 700; color: #15803d;">{remaining:.2f} €</div>
            </div>
        </div>
        <div style="margin-top: 8px;">
            <div style="background: #e5e7eb; border-radius: 999px; height: 8px; overflow: hidden;">
                <div style="background: #2563eb; height: 100%; width: {min(pct, 100)}%; border-radius: 999px;"></div>
            </div>
            <div style="font-size: 11px; color: #6b7280; margin-top: 4px;">{pct}% {_("invoiced")}</div>
        </div>
        """
        return format_html(html)
