from django.contrib import admin
from django.shortcuts import redirect
from django.urls import path
from invoices.views.invoice.upload_invoice import InvoiceUploadAdminView
from unfold.admin import ModelAdmin
from unfold.admin import GenericTabularInline
from invoices.models import (
    InvoiceGroup,
    Invoice,
    TaxType,
    AppliedTax,
    IncomingInvoice,
)

from invoices.tasks import process_incoming_invoices


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
    
