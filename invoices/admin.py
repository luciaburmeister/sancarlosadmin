from django.contrib import admin
from unfold.decorators import action
from django.shortcuts import redirect
from django.urls import path, reverse
from django.utils.translation import gettext_lazy as _
from invoices.views.invoice.upload_invoice import InvoiceUploadAdminView
from unfold.admin import ModelAdmin, TabularInline, GenericTabularInline
from django.utils.html import format_html
from invoices.models import (
    InvoiceGroup,
    Invoice,
    TaxType,
    AppliedTax,
    IncomingInvoice,
    SalesInvoice,
    SalesInvoiceItem,
)
from invoices.views.sales_invoice_pdf import SalesInvoicePdfView

from invoices.tasks import process_incoming_invoices, process_manual_invoice

@admin.register(InvoiceGroup)
class InvoiceGroupAdmin(ModelAdmin):
    list_display = ("name",)
    search_fields = ("name",)
    ordering = ("name",)

@admin.register(TaxType)
class TaxTypeAdmin(ModelAdmin):
    list_display = (
        "tax_name",
        "country",
        "behavior",
        "default_percentage",
        "is_active",
    )

    list_filter = ("country", "behavior", "is_active")
    search_fields = ("tax_name",)
    ordering = ("tax_name",)

    fieldsets = (
        (
            None,
            {
                "fields": (
                    "tax_name",
                    "country",
                    "behavior",
                    "is_active",
                )
            },
        ),
        (
            "Rates",
            {
                "fields": (
                    "percentage_options",
                    "default_percentage",
                ),
                "description": "Allowed rates and default percentage",
            },
        ),
    )

@admin.register(IncomingInvoice)
class IncomingInvoiceAdmin(ModelAdmin):
    list_display = ("file_hash", "company", "sender_email", "created_at", "processed")
    list_filter = ("company", "processed", "created_at")
    search_fields = ("file_hash", "source_email_id", "sender_email")
    readonly_fields = ("file_hash", "source_email_id", "created_at", "sender_email")
    autocomplete_fields = ("company", "email_config")
    actions = ["process_selected_invoices"]

    @admin.action(description="Process selected invoices with AI")
    def process_selected_invoices(self, request, queryset):
        
        selected_ids = list(queryset.values_list('id', flat=True))
        process_incoming_invoices.delay(invoice_ids=selected_ids)
        self.message_user(request, f"Started processing {len(selected_ids)} invoices in background.")

class AppliedTaxInline(GenericTabularInline):
    model = AppliedTax
    extra = 0
    hide_ordering_field = True
    ordering_field = "order" 
    autocomplete_fields = ("tax_type",)
    ordering = ("order",)
    tab=True

    fields = (
        "tax_type",
        "selected_percentage",
        "base_amount",
        "calculated_amount",
        "order",
    )

    verbose_name = "Applied tax"
    verbose_name_plural = "Applied taxes"

@admin.register(Invoice)
class InvoiceAdmin(ModelAdmin):
    list_display = (
        "invoice_number",
        "provider",
        "date_issued",
        "due_date",
        "payment_method",
        "group",
        "has_file",
        "ocr_status"
    )

    list_filter = (
        "payment_method",
        "group",
        "date_issued",
    )

    search_fields = (
        "invoice_number",
        "provider__name",
    )

    autocomplete_fields = (
        "provider",
        "group",
    )

    date_hierarchy = "date_issued"

    inlines = [
        AppliedTaxInline,
    ]
    actions = ["send_to_process"]

    fieldsets = (
        (
            "Invoice Info",
            {
                "fields": (
                   ( "invoice_number",
                    "provider"),
                    "file",
                )
            },
        ),
        (
            "Dates",
            {
                "fields": (
                    ("date_issued",
                    "due_date"),
                )
            },
        ),
        (
            "Payment",
            {
                "fields": (
                    "payment_method",
                )
            },
        ),
        (
            "Notes",
            {
                "fields": (
                    "group",
                    "observations",
                )
            },
        ),
    )

    def get_urls(self):
        urls = super().get_urls()
        custom = [
            path(
                "upload/",
                self.admin_site.admin_view(
                    InvoiceUploadAdminView.as_view(model_admin=self)
                ),
                name="invoices_invoice_upload",
            ),           
        ]
        return custom + urls

    def add_view(self, request, form_url="", extra_context=None):
        return redirect("admin:invoices_invoice_upload")

    @admin.display(boolean=True, description="Has File")
    def has_file(self, obj):
        return bool(obj.file)
    

    @admin.display(description="OCR Status")
    def ocr_status(self, obj):
        # Determine label + color
        if getattr(obj, "ocr_processing", False):
            label = "Processing"
            color = "#facc15" 
        elif getattr(obj, "ocr_processed", False) and not getattr(obj, "ocr_error", ""):
            label = "OK"
            color = "#22c55e"  
        elif getattr(obj, "ocr_error", ""):
            label = "Error"
            color = "#ef4444"  
        else:
            label = "Not started"
            color = "#9ca3af"  

        return format_html(
            '<span style="display:inline-block; padding:2px 8px; border-radius:999px; '
            'font-size:11px; font-weight:600; color:white; background:{};">{}</span>',
            color,
            label,
        )
    
    @action(description="Send selected invoices to AI processing")
    def send_to_process(self, request, queryset):
        selected_ids = list(queryset.values_list('id', flat=True))
        for id in selected_ids:
            process_manual_invoice.delay(invoice_id=id)
        self.message_user(request, f"Started processing {len(selected_ids)} invoices in background.")

        
class SalesInvoiceItemInline(TabularInline):
    model = SalesInvoiceItem
    extra = 0
    tab = True

@admin.register(SalesInvoice)
class SalesInvoiceAdmin(ModelAdmin):
    list_display = ("invoice_number", "company", "client", "date_issued", "total_amount")
    list_filter = ("company", "date_issued")
    search_fields = ("invoice_number", "client__name")
    autocomplete_fields = ("company", "client", "configuration")
    inlines = [SalesInvoiceItemInline, AppliedTaxInline]
    
    actions = ["download_pdf"]
    actions_row = ["view_pdf_row_action"]
    actions_detail = ["view_pdf_detail_action"]

    @action(description=_("View PDF"), icon="visibility")
    def view_pdf_row_action(self, request, object_id):
        return redirect(reverse("admin:invoices_salesinvoice_pdf", args=[object_id]))

    @action(description=_("View PDF"), icon="visibility")
    def view_pdf_detail_action(self, request, object_id):
        return redirect(reverse("admin:invoices_salesinvoice_pdf", args=[object_id]))

    def download_pdf(self, request, queryset):
        if queryset.count() != 1:
            self.message_user(request, _("Please select only one invoice to download."), level="error")
            return
        
        invoice = queryset.first()
        return redirect(reverse("admin:invoices_salesinvoice_pdf", args=[invoice.id]))

    download_pdf.short_description = _("Download PDF")

    def get_urls(self):
        urls = super().get_urls()
        custom = [
            path(
                "<int:object_id>/pdf/",
                self.admin_site.admin_view(SalesInvoicePdfView.as_view(model_admin=self)),
                name="invoices_salesinvoice_pdf",
            ),
        ]
        return custom + urls
