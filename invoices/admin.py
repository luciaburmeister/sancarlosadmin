from django.contrib import admin
from unfold.admin import ModelAdmin
from django.contrib.contenttypes.admin import GenericTabularInline
from invoices.models import (
    InvoiceGroup,
    Invoice,
    TaxType,
    AppliedTax,
)

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

class AppliedTaxInline(GenericTabularInline):
    model = AppliedTax
    extra = 0

    autocomplete_fields = ("tax_type",)
    ordering = ("order",)

    fields = (
        "tax_type",
        "selected_percentage",
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
            None,
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