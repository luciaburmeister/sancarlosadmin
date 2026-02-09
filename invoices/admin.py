from django.contrib import admin
from unfold.admin import ModelAdmin
from invoices.models import (
    InvoiceGroup,
    Invoice,
    TaxType,
    AppliedTax,
    IncomingInvoice,
)
from unfold.admin import GenericTabularInline
from company.models import CompanyEmailConfig

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

class AppliedTaxInline(GenericTabularInline):
    model = AppliedTax
    extra = 0
    hide_ordering_field = True
    ordering_field = "order" 
    autocomplete_fields = ("tax_type",)
    ordering = ("order",)

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
            "File",  
            {
                "fields": (
                    "file",
                )
            },
        ),
        (
            "Invoice Info",
            {
                "fields": (
                    "invoice_number",
                    "provider",
                    "group",
                )
            },
        ),
        (
            "Dates",
            {
                "fields": (
                    "date_issued",
                    "due_date",
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
                    "observations",
                )
            },
        ),
    )
    # shows check or X depending on whether file is present
    @admin.display(boolean=True, description="Has File")
    def has_file(self, obj):
        return bool(obj.file)