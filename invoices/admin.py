from unfold.admin import StackedInline
from django.contrib import admin
from unfold.decorators import action, display
from django.shortcuts import redirect
from django.urls import path, reverse
from django.utils.translation import gettext_lazy as _
from datetime import timedelta
from invoices.views.invoice.upload_invoice import InvoiceUploadAdminView
from unfold.admin import ModelAdmin, TabularInline, GenericTabularInline
from django.utils.html import format_html
from invoices.models import (
    Invoice,
    TaxType,
    AppliedTax,
    IncomingInvoice,
    SalesInvoice,
    SalesInvoiceItem,
    SalesInvoiceStatus,
    InvoiceItem
)
from django.utils import timezone
from invoices.views.sales_invoice_pdf import SalesInvoicePdfView
from invoices.views.invoice.wizard_views import SalesInvoiceWizardView
from invoices.views.invoice.autocomplete_views import TaxTypeAutocompleteView
from invoices.services.sales_invoice_service import SalesInvoiceService
from invoices.tasks import process_incoming_invoices, process_manual_invoice
from accounting.tasks import classify_invoice_accounts, generate_journal_entries_task, export_a3_purchase_invoicing_task
from core.admin.company_admin import CompanyRestrictedAdmin
from unfold.contrib.filters.admin import (
    AutocompleteSelectFilter,
    AutocompleteSelectMultipleFilter
)
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
    actions = ["merge_tax_types"] 

    fieldsets = (
        (
            None,
            {
                "fields": (
                    "tax_name",
                    "country",
                    ("behavior","is_debit"),
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

    @admin.action(description="Merge selected tax types (choose which to keep)")
    def merge_tax_types(self, request, queryset):
        if queryset.count() < 2:
            self.message_user(
                request,
                "Please select exactly 2 tax types to merge.",
                level="error"
            )
            return
        ids = ",".join(str(i) for i in queryset.values_list("id", flat=True))
        return redirect(reverse("admin:invoices_taxtype_merge") + f"?ids={ids}")

@admin.register(IncomingInvoice)
class IncomingInvoiceAdmin(CompanyRestrictedAdmin,ModelAdmin):
    list_display = ("file_hash", "company", "sender_email", "created_at", "processed")
    list_filter = ("company", "processed", "created_at")
    search_fields = ("file_hash", "source_email_id", "sender_email")
    readonly_fields = ("file_hash", "source_email_id", "created_at", "sender_email")
    autocomplete_fields = ("company", "email_config")
    actions = ["process_selected_invoices"]

    @admin.action(description="Process selected invoices with AI")
    def process_selected_invoices(self, request, queryset):
        selected_ids = list(queryset.values_list('id', flat=True))
        for id in selected_ids:
            process_incoming_invoices.delay(invoice_ids=[id])
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

class InvoiceItemInline(StackedInline):
    model = InvoiceItem
    extra = 0
    tab = True
    readonly_fields = ("line_total",)
    autocomplete_fields = ("account",)
    fieldsets = (
        (
            None,
            {
                "fields": (
                    "description",
                    ("quantity", "unit_price", "line_total"),
                    "account",
                )
            },
        ),
    )

@admin.register(Invoice)
class InvoiceAdmin(CompanyRestrictedAdmin,ModelAdmin):
    list_display = (
        "invoice_number",
        "provider",
        "date_issued",
        "due_date",
        "ocr_status"
    )

    list_filter = (
        "payment_method",
        ("company",AutocompleteSelectMultipleFilter),
        ("provider",AutocompleteSelectMultipleFilter),
        "date_issued",
    )

    search_fields = (
        "invoice_number",
        "provider__name",
    )

    autocomplete_fields = (
        "provider",
        "account",
        "project_file",
    )

    date_hierarchy = "date_issued"

    inlines = [
        AppliedTaxInline,
        InvoiceItemInline
    ]

    actions = ["send_to_process", "classify_accounts", "journalize_selected", "export_a3_purchase_invoices"]
    actions_row = ["view_journal_entry_row_action"]
    actions_detail = ["view_journal_entry_detail_action"]

    fieldsets = (
        (
            "Invoice Info",
            {
                "fields": (
                    ("invoice_number", "provider"),
                    "file",
                    "company",
                    "project_file",
                )
            },
        ),
        (
            "Dates",
            {
                "fields": (
                    ("date_issued", "due_date"),
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
                    "account",
                    "observations",
                )
            },
        ),
    )

    def save_related(self, request, form, formsets, change):
        super().save_related(request, form, formsets, change)
        form.instance.recalculate_totals(save=True)

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

    @action(description=_("Journal Entry"), icon="leaderboard")
    def view_journal_entry_row_action(self, request, object_id):
        url = reverse('admin:accounting_journalentry_changelist') + f"?purchase_invoice__id={object_id}"
        return redirect(url)

    @action(description=_("Journal Entry"), icon="leaderboard")
    def view_journal_entry_detail_action(self, request, object_id):
        url = reverse('admin:accounting_journalentry_changelist') + f"?purchase_invoice__id={object_id}"
        return redirect(url)
    
    @action(description="Send selected invoices to AI processing")
    def send_to_process(self, request, queryset):
        selected_ids = list(queryset.values_list('id', flat=True))
        for id in selected_ids:
            process_manual_invoice.delay(invoice_id=id)
        self.message_user(request, f"Started processing {len(selected_ids)} invoices in background.")

    @action(description="Define accounting account for selected invoices")
    def classify_accounts(self, request, queryset):
        selected_ids = list(queryset.values_list('id', flat=True))
        classify_invoice_accounts.delay(invoice_ids=selected_ids)
        self.message_user(request, f"Started classification for {len(selected_ids)} invoices.")

    @action(description="Generate accounting journal entries for selected company")
    def journalize_selected(self, request, queryset):
        # We find distinct companies from the selected invoices
        company_ids = set(queryset.values_list('company_id', flat=True))
        for cid in company_ids:
            generate_journal_entries_task.delay(company_id=cid)
        self.message_user(request, f"Started journal entry generation for {len(company_ids)} companies.")

    @action(description="Export Purchase Invoices to A3 (Last 90 days)")
    def export_a3_purchase_invoices(self, request, queryset):
        company_ids = {cid for cid in queryset.values_list('company_id', flat=True) if cid is not None}
        end_date = timezone.now().date()
        start_date = end_date - timedelta(days=90)
        
        for cid in company_ids:
            export_a3_purchase_invoicing_task.delay(
                company_id=cid,
                user_email=request.user.email,
                start_date_str=start_date.isoformat(),
                end_date_str=end_date.isoformat()
            )
        self.message_user(request, f"Started A3 export for {len(company_ids)} companies. You will receive an email shortly.")
        
class SalesInvoiceItemInline(TabularInline):
    model = SalesInvoiceItem
    extra = 0
    tab = True
    fields = ("description", "quantity", "unit_price", "discount_amount", "line_total")
    readonly_fields = ("line_total",)

@admin.register(SalesInvoice)
class SalesInvoiceAdmin(CompanyRestrictedAdmin,ModelAdmin):
    custom_urls = (
        (
            "autocomplete/taxtype/",
            "invoices_taxtype_autocomplete",
            TaxTypeAutocompleteView.as_view(),
        ),
    )
    list_display = ("invoice_number", "incremental_id", "get_status_label", "company", "client", "date_issued", "total_amount")
    list_filter = ("company", "status", "date_issued")
    search_fields = ("invoice_number", "client__name")
    autocomplete_fields = ("company", "client", "configuration", "project_file")
    inlines = [SalesInvoiceItemInline, AppliedTaxInline]
    
    readonly_fields = ("incremental_id", "subtotal_amount", "tax_amount", "total_amount","status","invoice_number")

    fieldsets = (
        (
            _("General Information"),
            {
                "fields": (
                    ("company", "client"),
                    ("status", "invoice_number"),
                    ("date_issued", "due_date"),
                    "project_file",
                    ("reference"),
                )
            },
        ),
        (
            _("Configuration"),
            {
                "fields": (
                    "configuration",
                )
            },
        ),
        (
            _("Financial Summary"),
            {
                "fields": (
                    "subtotal_amount",
                    "tax_amount",
                    "total_amount",
                ),
            },
        ),
        (
            _("Additional Notes"),
            {
                "fields": ("notes",),
            },
        ),
    )
    
    def save_related(self, request, form, formsets, change):
        super().save_related(request, form, formsets, change)
        form.instance.recalculate_totals(save=True)
    
    actions = ["download_pdf", "finalize_invoice_action"]
    actions_row = ["view_pdf_row_action", "finalize_invoice_row_action", "view_journal_entry_row_action"]
    actions_detail = ["view_pdf_detail_action", "finalize_invoice_detail_action", "view_journal_entry_detail_action"]

    @action(description=_("View PDF"), icon="visibility", attrs={"target": "_blank"})
    def view_pdf_row_action(self, request, object_id):
        return redirect(reverse("admin:invoices_salesinvoice_pdf", args=[object_id]))

    @action(description=_("View PDF"), icon="visibility", attrs={"target": "_blank"})
    def view_pdf_detail_action(self, request, object_id):
        return redirect(reverse("admin:invoices_salesinvoice_pdf", args=[object_id]))

    @action(description=_("Finalize"), icon="gavel")
    def finalize_invoice_row_action(self, request, object_id):
        invoice = SalesInvoice.objects.get(pk=object_id)
        if invoice.status == SalesInvoiceStatus.FINAL:
            self.message_user(request, _("Invoice is already final."), level="warning")
        else:
            SalesInvoiceService.finalize_invoice(invoice)
            self.message_user(request, _("Invoice finalized and Delivery Order generated."))
        return redirect(reverse("admin:invoices_salesinvoice_changelist"))

    @action(description=_("Finalize"), icon="gavel")
    def finalize_invoice_detail_action(self, request, object_id):
        invoice = SalesInvoice.objects.get(pk=object_id)
        if invoice.status == SalesInvoiceStatus.FINAL:
            self.message_user(request, _("Invoice is already final."), level="warning")
        else:
            SalesInvoiceService.finalize_invoice(invoice)
            self.message_user(request, _("Invoice finalized and Delivery Order generated."))
        return redirect(reverse("admin:invoices_salesinvoice_change", args=[object_id]))

    @display(
        description=_("Status"),
        ordering="status",
        label={
            _(SalesInvoiceStatus.FINAL): "success",  
            _(SalesInvoiceStatus.PROFORMA): "warning",  
        },
    )
    def get_status_label(self, obj):
        return obj.status

    @admin.action(description=_("Finalize selected pro forma invoices"))
    def finalize_invoice_action(self, request, queryset):
        count = 0
        for invoice in queryset.filter(status=SalesInvoiceStatus.PROFORMA):
            SalesInvoiceService.finalize_invoice(invoice)
            count += 1
        self.message_user(request, _("Finalized %d invoices and generated Delivery Orders.") % count)

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
                "add/",
                self.admin_site.admin_view(SalesInvoiceWizardView.as_view(model_admin=self)),
                name="invoices_salesinvoice_wizard",
            ),
            path(
                "<int:object_id>/pdf/",
                self.admin_site.admin_view(SalesInvoicePdfView.as_view(model_admin=self)),
                name="invoices_salesinvoice_pdf",
            ),
        ]
        return custom + urls

    def add_view(self, request, form_url="", extra_context=None):
        return redirect("admin:invoices_salesinvoice_wizard")

    @action(description=_("Journal Entry"), icon="leaderboard")
    def view_journal_entry_row_action(self, request, object_id):
        url = reverse('admin:accounting_journalentry_changelist') + f"?sales_invoice__id={object_id}"
        return redirect(url)

    @action(description=_("Journal Entry"), icon="leaderboard")
    def view_journal_entry_detail_action(self, request, object_id):
        url = reverse('admin:accounting_journalentry_changelist') + f"?sales_invoice__id={object_id}"
        return redirect(url)
