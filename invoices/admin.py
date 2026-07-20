from unfold.contrib.filters.admin import BooleanRadioFilter
import logging

from assets import urls
from unfold.admin import StackedInline
from django.contrib import admin
from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect
from unfold.decorators import action, display
from django.urls import path, reverse

logger = logging.getLogger(__name__)
from django.utils.translation import gettext_lazy as _
from datetime import timedelta
from unfold.admin import ModelAdmin, GenericTabularInline, TabularInline
from django.utils.html import format_html
from django.db import models
from invoices.views.invoice.aging_export_view import AgingExportView

from invoices.models import (
    Invoice,
    ConfirmationStatus,
    TaxType,
    AppliedTax,
    IncomingInvoice,
    InvoicePaymentStatus,
    SalesInvoice,
    SalesInvoiceItem,
    SalesInvoiceStatus,
    InvoiceItem,
    InvoicePOAllocation
)
from invoices.views.invoice.aging_export_view import AgingExportView
from payments.models import PaymentAllocation
from django.utils import timezone
from invoices.services.invoice_pdf_download import InvoicePdfDownloadService
from invoices.tasks import build_invoice_pdf_zip_task
from invoices.views.invoice_pdf_zip_status import (
    InvoicePdfZipDownloadView,
    InvoicePdfZipStatusAPIView,
    InvoicePdfZipStatusPageView,
    redirect_to_pdf_zip_status,
)
from invoices.views.invoice.wizard_views import SalesInvoiceWizardView
from invoices.views.invoice.download_invoice_file import PurchaseInvoiceFileDownloadView
from invoices.views.sales_invoice_pdf import SalesInvoicePdfDownloadView, SalesInvoicePdfView
from invoices.views.invoice.import_sales_invoice import SalesInvoiceImportAdminView
from invoices.views.invoice.bulk_mark_paid import BulkMarkPaidAdminView
from invoices.views.invoice.bulk_mark_invoice_paid import BulkMarkInvoicePaidAdminView
from invoices.views.invoice.autocomplete_views import TaxTypeAutocompleteView
from invoices.views.sales_invoice_register_payment import RegisterPaymentView
from invoices.views.invoice.review_invoice import InvoiceReviewAdminView
from invoices.views.invoice.match_po import InvoiceMatchPOView
from invoices.views.invoice.upload_invoice import InvoiceUploadAdminView
from invoices.views.invoice.import_invoice import InvoiceImportAdminView
from invoices.views.invoice_register_payment import InvoiceRegisterPaymentView
from invoices.views.accounts_payable_views import AccountsPayableDashboardView
from invoices.views.operational_dashboard_view import OperationalDashboardView
from invoices.services.sales_invoice_service import SalesInvoiceService
from apps.einvoicing.admin import EInvoicingSubmissionInline
from apps.einvoicing.tasks import submit_invoice_to_aeat, cancel_invoice_at_aeat
from invoices.tasks import process_incoming_invoices, process_manual_invoice
from banking.tasks import run_invoice_matcher_task
from accounting.tasks import classify_invoice_accounts, generate_journal_entries_task, export_a3_purchase_invoicing_task
from core.tasks import send_invoice_email_task
from company.models import EmailLog
from core.admin.company_admin import CompanyRestrictedAdmin
from unfold.contrib.filters.admin import (
    RangeDateFilter,
    ChoicesDropdownFilter,
    AutocompleteSelectMultipleFilter
)
from unfold.widgets import UnfoldAdminTextareaWidget
from .filters import ItemsMissingAccountFilter, BalanceStatusFilter, IsReconciledFilter, DueDatePassedFilter
from import_export.admin import  ExportActionModelAdmin
from unfold.contrib.import_export.forms import  ImportForm, SelectableFieldsExportForm
from .resources import (
    InvoiceResource, 
    InvoiceItemResource, 
    SalesInvoiceResource, 
    SalesInvoiceItemResource
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
    autocomplete_fields = ("purchase_account", "sales_account")

    fieldsets = (
        (
            None,
            {
                "fields": (
                    "tax_name",
                    "country",
                    ("behavior", "is_debit"),
                    "a3_tax_group",
                    ("purchase_account", "sales_account"),
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
                "Please select at least 2 tax types to merge.",
                level="error"
            )
            return
        ids = ",".join(str(i) for i in queryset.values_list("id", flat=True))
        return redirect(reverse("admin:invoices_taxtype_merge") + f"?ids={ids}")

    def get_urls(self):
        from invoices.views.merge_tax_types_view import MergeTaxTypesView

        urls = super().get_urls()
        custom = [
            path(
                "merge/",
                self.admin_site.admin_view(
                    MergeTaxTypesView.as_view(model_admin=self)
                ),
                name="invoices_taxtype_merge",
            ),
        ]
        return custom + urls

@admin.register(IncomingInvoice)
class IncomingInvoiceAdmin(CompanyRestrictedAdmin,ModelAdmin):
    list_display = ("file_hash", "company", "sender_email", "created_at", "processed", "is_duplicate", "has_error")
    list_filter = ("company", "processed", "is_duplicate", "created_at")
    search_fields = ("file_hash", "source_email_id", "sender_email")
    readonly_fields = ("file_hash", "source_email_id", "created_at", "sender_email", "error")

    @admin.display(boolean=True, description="Error")
    def has_error(self, obj):
        return bool(obj.error)
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

    def has_add_permission(self, request, obj):
        if isinstance(obj, SalesInvoice) and obj.status == SalesInvoiceStatus.FINAL:
            return False
        return super().has_add_permission(request, obj)

    def has_change_permission(self, request, obj=None):
        if isinstance(obj, SalesInvoice) and obj.status == SalesInvoiceStatus.FINAL:
            return False
        return super().has_change_permission(request, obj)

    def has_delete_permission(self, request, obj=None):
        if isinstance(obj, SalesInvoice) and obj.status == SalesInvoiceStatus.FINAL:
            return False
        return super().has_delete_permission(request, obj)

class PaymentAllocationInline(GenericTabularInline):
    model = PaymentAllocation
    ct_field = "content_type"
    fk_field = "object_id"
    extra = 0
    tab = True
    verbose_name = _("Payment")
    verbose_name_plural = _("Payments")
    fields = ("get_payment_date", "get_method", "get_reference", "amount_applied", "get_status", "get_payment_link")
    readonly_fields = ("get_payment_date", "get_method", "get_reference", "amount_applied", "get_status", "get_payment_link")
    can_delete = False

    def get_payment_date(self, obj):
        return obj.payment.payment_date
    get_payment_date.short_description = _("Payment Date")

    def get_payment_link(self, obj):
        from payments.models import PaymentCreatedBy

        url = reverse("admin:payments_payment_change", args=[obj.payment_id])
        label = _("Edit") if obj.payment.created_by == PaymentCreatedBy.MANUAL else _("View")
        return format_html('<a href="{}" class="text-primary-600 underline">{}</a>', url, label)
    get_payment_link.short_description = _("Payment")

    def get_method(self, obj):
        return obj.payment.get_method_display()
    get_method.short_description = _("Method")

    def get_reference(self, obj):
        return obj.payment.reference or "—"
    get_reference.short_description = _("Reference")

    def get_status(self, obj):
        return obj.payment.get_status_display()
    get_status.short_description = _("Status")

    def get_queryset(self, request):
        return super().get_queryset(request).select_related("payment")


class InvoiceItemInline(StackedInline):
    model = InvoiceItem
    extra = 0
    tab = True
    readonly_fields = ("discount_amount", "gross_total", "line_total")
    autocomplete_fields = ("account",)
    fieldsets = (
        (
            None,
            {
                "fields": (
                    "description",
                    ("quantity", "unit_price"),
                    ("gross_total", "discount_percentage", "discount_amount", "line_total"),
                    "account",
                )
            },
        ),
    )

class InvoicePOAllocationInline(TabularInline):
    """
    Read-only overview of PO matches. Allocations are created via the
    "Match Purchase Order" action (matcher flow); deleting here is the
    escape hatch to undo a wrong match.
    """
    model = InvoicePOAllocation
    fk_name = "invoice"
    extra = 0
    tab = True
    verbose_name = _("PO Allocation")
    verbose_name_plural = _("Purchase Orders")
    fields = ("invoice_item", "get_purchase_order", "po_item", "quantity")
    readonly_fields = ("invoice_item", "get_purchase_order", "po_item", "quantity")

    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    @display(description=_("Purchase Order"))
    def get_purchase_order(self, obj):
        po = obj.po_item.purchase_order
        url = reverse("admin:purchasing_purchaseorder_change", args=[po.pk])
        return format_html('<a href="{}" class="text-primary-600 underline">{}</a>', url, po.number)

    def get_queryset(self, request):
        return super().get_queryset(request).select_related(
            "invoice_item", "po_item__purchase_order"
        )


@admin.register(Invoice)
class InvoiceAdmin(ModelAdmin, CompanyRestrictedAdmin, ExportActionModelAdmin):
    resource_classes = [InvoiceResource, InvoiceItemResource]
    import_form_class = ImportForm
    export_form_class = SelectableFieldsExportForm

    list_display = (
        "invoice_number",
        "provider",
        "total_amount",
        "date_issued",
        "due_date",
        "created_at",
        "ocr_status",
        "balance_status",
        "get_payment_status_label",
        "confirmation_badge",
    )

    list_filter = (
        # Who / when
        ("company", AutocompleteSelectMultipleFilter),
        ("provider", AutocompleteSelectMultipleFilter),
        ("date_issued", RangeDateFilter),
        # Document state
        ("confirmation_status", ChoicesDropdownFilter),
        # Payment
        ("payment_status", ChoicesDropdownFilter),
        DueDatePassedFilter,
        ("payment_method", ChoicesDropdownFilter),
        IsReconciledFilter,
        # Accounting diagnostics
        BalanceStatusFilter,
        ItemsMissingAccountFilter,
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

    readonly_fields = (
        "net_amount",
        "total_amount",
        "gemini_account_response",
        "ocr_raw_response",
        "ocr_error",
        "amount_paid",
        "amount_remaining",
    )

    list_select_related = (
        "provider",
        "company",
    )

    date_hierarchy = "date_issued"
    
    list_filter_submit = True

    inlines = [
        AppliedTaxInline,
        InvoiceItemInline,
        InvoicePOAllocationInline,
        PaymentAllocationInline,
    ]

    actions = [
        "download_pdf",
        "send_to_process",
        "classify_accounts",
        "journalize_selected",
        "export_a3_purchase_invoices",
        "import_from_excel",
        "run_matcher_bulk",
        "bulk_mark_as_paid",
    ]
    actions_row = [
        "review_invoice_row_action",
        "download_pdf_row_action",
        "view_journal_entry_row_action",
    ]
    actions_detail = [
        {
            "title": _("Invoice Actions"),
            "items": [
                "download_pdf_detail_action",
                "register_payment_detail_action",
                "run_invoice_matcher_detail_action",
                "match_po_detail_action",
            ],
        },
        {
            "title": _("Accounting"),
            "items": [
                "view_journal_entry_detail_action",
            ],
        },
    ]

    @action(description=_("Match Purchase Order"), icon="link")
    def match_po_detail_action(self, request, object_id):
        return redirect(reverse("admin:invoices_invoice_match_po", args=[object_id]))

    @action(description=_("Review"), icon="rate_review")
    def review_invoice_row_action(self, request, object_id):
        return redirect(reverse("admin:invoices_invoice_review", args=[object_id]))

    @action(description=_("Download PDF"), icon="download")
    def download_pdf_row_action(self, request, object_id):
        return redirect(reverse("admin:invoices_invoice_download_pdf", args=[object_id]))

    @action(description=_("Download PDF"), icon="download")
    def download_pdf_detail_action(self, request, object_id):
        return redirect(reverse("admin:invoices_invoice_download_pdf", args=[object_id]))

    @admin.action(description=_("Download PDF"))
    def download_pdf(self, request, queryset):
        invoice_ids = list(queryset.values_list("pk", flat=True))
        if not invoice_ids:
            self.message_user(
                request,
                _("Select at least one invoice."),
                level=messages.ERROR,
            )
            return None

        # Single selection: download immediately. Multiple: Celery + status page.
        if len(invoice_ids) == 1:
            invoice = queryset.first()
            content = InvoicePdfDownloadService.read_purchase_invoice_file(invoice)
            if content is None:
                self.message_user(
                    request,
                    _("This invoice has no attached file."),
                    level=messages.ERROR,
                )
                return None
            filename = InvoicePdfDownloadService.purchase_invoice_filename(invoice)
            lower = filename.lower()
            content_type = "application/pdf"
            if lower.endswith(".png"):
                content_type = "image/png"
            elif lower.endswith(".jpg") or lower.endswith(".jpeg"):
                content_type = "image/jpeg"
            return InvoicePdfDownloadService.pdf_attachment_response(
                content, filename, content_type=content_type
            )

        result = build_invoice_pdf_zip_task.delay(
            "purchase", invoice_ids, user_id=request.user.id
        )
        return redirect_to_pdf_zip_status(
            request,
            result.id,
            reverse("admin:invoices_invoice_changelist"),
        )

    @action(description=_("Run Matcher"), url_path="run-matcher", icon="compare_arrows")
    def run_invoice_matcher_detail_action(self, request, object_id):
        from banking.tasks import run_invoice_matcher_task

        invoice = get_object_or_404(Invoice, pk=object_id)

        # Dispatch to Celery and redirect to the status page
        result = run_invoice_matcher_task.delay("invoice", invoice.pk)
        back_url = reverse("admin:invoices_invoice_change", args=[invoice.pk])
        status_url = (
            reverse("admin:banking_matcher_status")
            + f"?task_id={result.id}&task_type=invoice&back_url={back_url}"
        )
        return redirect(status_url)

    fieldsets = (
        (
            _("Invoice Info"),
            {
                "fields": (
                    ("invoice_number", "provider"),
                    ("net_amount", "total_amount"),
                    "file",
                    "company",
                    "project_file",
                )
            },
        ),
        (
            _("Dates"),
            {
                "fields": (
                    ("date_issued", "due_date"),
                    "accounting_date",
                )
            },
        ),
        (
            _("Payment"),
            {
                "fields": (
                    "payment_method",
                )
            },
        ),
        (
            _("Notes"),
            {
                "fields": (
                    "account",
                    "observations",
                )
            },
        ),
        (
            _("Payment Tracking"),
            {
                "fields": (
                    "payment_status",
                    "amount_paid",
                    "amount_remaining",
                ),
            },
        ),
        (
            _("AI Classification"),
            {
                "fields": (
                    "gemini_account_response",
                    "ocr_raw_response",
                    "ocr_error",
                )
            },
        ),
    )

    def save_related(self, request, form, formsets, change):
        super().save_related(request, form, formsets, change)
        form.instance.recalculate_totals(save=True)

        mismatch = form.instance.get_balance_mismatch()
        if mismatch:
            calculated_total, manual_total = mismatch
            self.message_user(
                request,
                _(
                    "Warning: the calculated total (%(calculated)s) does not match "
                    "the invoice's recorded total (%(manual)s). Saved anyway — please "
                    "review the line items, taxes, or the gross amount."
                ) % {"calculated": calculated_total, "manual": manual_total},
                level=messages.WARNING,
            )

    def get_urls(self):
        urls = super().get_urls()
        custom = [
            path(
                "dashboard/accounts-payable/",
                self.admin_site.admin_view(
                    AccountsPayableDashboardView.as_view(model_admin=self)
                ),
                name="invoices_invoice_accounts_payable",
            ),
            path(
                "dashboard/operational/",
                self.admin_site.admin_view(
                    OperationalDashboardView.as_view(model_admin=self)
                ),
                name="invoices_invoice_operational_dashboard",
            ),
            path(
                "upload/",
                self.admin_site.admin_view(
                    InvoiceUploadAdminView.as_view(model_admin=self)
                ),
                name="invoices_invoice_upload",
            ), 
            path(
                "import/",
                self.admin_site.admin_view(
                    InvoiceImportAdminView.as_view(model_admin=self)
                ),
                name="invoices_invoice_import",
            ),
            path(
                "pdf-zip-status/",
                self.admin_site.admin_view(
                    InvoicePdfZipStatusPageView.as_view(model_admin=self)
                ),
                name="invoices_pdf_zip_status",
            ),
            path(
                "pdf-zip-status/<str:task_id>/api/",
                self.admin_site.admin_view(
                    InvoicePdfZipStatusAPIView.as_view(model_admin=self)
                ),
                name="invoices_pdf_zip_status_api",
            ),
            path(
                "pdf-zip-status/<str:task_id>/download/",
                self.admin_site.admin_view(
                    InvoicePdfZipDownloadView.as_view(model_admin=self)
                ),
                name="invoices_pdf_zip_download",
            ),
            path(
            "<int:object_id>/review/",
            self.admin_site.admin_view(
                InvoiceReviewAdminView.as_view(model_admin=self)
            ),
            name="invoices_invoice_review",
            ),
            path(
                "<int:object_id>/download-pdf/",
                self.admin_site.admin_view(
                    PurchaseInvoiceFileDownloadView.as_view(model_admin=self)
                ),
                name="invoices_invoice_download_pdf",
            ),
            path(
                "<int:object_id>/register-payment/",
                self.admin_site.admin_view(
                    InvoiceRegisterPaymentView.as_view(model_admin=self)
                ),
                name="invoices_invoice_register_payment",
            ),
            path(
                "<int:object_id>/match-po/",
                self.admin_site.admin_view(
                    InvoiceMatchPOView.as_view(model_admin=self)
                ),
                name="invoices_invoice_match_po",
            ),
            path(
                "bulk-mark-paid/",
                self.admin_site.admin_view(
                    BulkMarkInvoicePaidAdminView.as_view(model_admin=self)
                ),
                name="invoices_invoice_bulk_mark_paid",
            ),
            path(
                "dashboard/operational/aging-export/",
                self.admin_site.admin_view(
                    AgingExportView.as_view(model_admin=self)
                ),
                name="invoices_invoice_aging_export",
),
            path(
                "bulk-mark-paid/",
                self.admin_site.admin_view(
                    BulkMarkInvoicePaidAdminView.as_view(model_admin=self)
                ),
                name="invoices_invoice_bulk_mark_paid",
            ),
        ]
        return custom + urls

    def add_view(self, request, form_url="", extra_context=None):
        return redirect("admin:invoices_invoice_upload")

    @admin.display(boolean=True, description="Has File")
    def has_file(self, obj):
        return bool(obj.file)

    @display(
        description=_("OCR Status"),
        label={
            "Processing":  "warning",
            "OK":          "success",
            "Error":       "danger",
            "Not started": "default",
        },
    )
    def ocr_status(self, obj):
        if getattr(obj, "ocr_processing", False):
            return "Processing"
        elif getattr(obj, "ocr_processed", False) and not getattr(obj, "ocr_error", ""):
            return "OK"
        elif getattr(obj, "ocr_error", ""):
            return "Error"
        else:
            return "Not started"
        
    @display(description=_("Balance"),
    label={
            _("Balanced"): "success",  # green
            _("Unbalanced"): "danger",  # red
        },)
    def balance_status(self, obj):
        if obj.is_total_balanced:
            return _("Balanced")
        else:
            return _("Unbalanced")
        
    @display(
        description=_("Confirmation"),
        label={
            "APPROVED":       "success",
            "PENDING REVIEW": "warning",
            "DECLINED":       "danger",
        },
    )
    def confirmation_badge(self, obj):
        labels = {
            ConfirmationStatus.APPROVED: "APPROVED",
            ConfirmationStatus.PENDING:  "PENDING REVIEW",
            ConfirmationStatus.DECLINED: "DECLINED",
        }
        return labels.get(obj.confirmation_status, "PENDING REVIEW")

    @display(
        description=_("Payment"),
        ordering="payment_status",
        label={
            _("Unpaid"): "danger",
            _("Partial"): "warning",
            _("Paid"): "success",
        },
    )
    def get_payment_status_label(self, obj):
        return obj.get_payment_status_display()

    @action(description=_("Register Payment"), icon="payments")
    def register_payment_detail_action(self, request, object_id):
        return redirect(reverse("admin:invoices_invoice_register_payment", args=[object_id]))

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

    @admin.action(description=_("Import invoices from Excel"))
    def import_from_excel(self, request, queryset):
        return redirect(reverse("admin:invoices_invoice_import"))

    @admin.action(description=_("Run bank matcher on selected invoices"))
    def run_matcher_bulk(self, request, queryset):
        from django.utils.html import format_html
        from django.utils.safestring import mark_safe

        ids = list(queryset.values_list("id", flat=True))
        for invoice_id in ids:
            run_invoice_matcher_task.delay("invoice", invoice_id)
        suggestions_url = reverse("admin:banking_invoicereconciliationsuggestion_changelist") + "?is_confirmed=False"
        self.message_user(
            request,
            mark_safe(format_html(
                _("Queued bank matcher for {count} invoice(s). Results will appear in <a href=\"{url}\">Reconciliation Suggestions</a> shortly."),
                count=len(ids),
                url=suggestions_url,
            )),
        )

    @admin.action(
        description=_("Bulk update payment status"),
        permissions=["bulk_mark_paid_invoice"],
    )
    def bulk_mark_as_paid(self, request, queryset):
        return redirect(reverse("admin:invoices_invoice_bulk_mark_paid"))

    def has_bulk_mark_paid_invoice_permission(self, request):
        return request.user.has_perm("invoices.bulk_mark_paid_invoice")

class SalesInvoiceItemInline(StackedInline):
    model = SalesInvoiceItem
    extra = 0
    tab = True
    readonly_fields = ("discount_amount", "line_total", "source_id", "project_file")
    formfield_overrides = {
        models.CharField: {"widget": UnfoldAdminTextareaWidget(attrs={"rows": 2})},
    }
    fieldsets = (
        (
            _("Line Item"),
            {
                "fields": (
                    ("quantity",
                    "unit_price"),
                    ("discount_percentage", "discount_amount"), 
                    "description",
                    ("reference", "line_total", "source_id"),
                    "project_file",
                )
            },
        ),
    )

    def has_add_permission(self, request, obj):
        if obj and obj.status == SalesInvoiceStatus.FINAL:
            return False
        return super().has_add_permission(request, obj)

    def has_change_permission(self, request, obj=None):
        if obj and obj.status == SalesInvoiceStatus.FINAL:
            return False
        return super().has_change_permission(request, obj)

    def has_delete_permission(self, request, obj=None):
        if obj and obj.status == SalesInvoiceStatus.FINAL:
            return False
        return super().has_delete_permission(request, obj)

class EmailLogInline(GenericTabularInline):
    model = EmailLog
    extra = 0
    tab = True
    verbose_name = _("Email Sent")
    verbose_name_plural = _("Emails Sent")
    fields = ("to_email", "subject", "sent_at", "status", "error_message")
    readonly_fields = ("to_email", "subject", "sent_at", "status", "error_message")
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False

@admin.register(SalesInvoice)
class SalesInvoiceAdmin(ModelAdmin, CompanyRestrictedAdmin, ExportActionModelAdmin):
    resource_classes = [SalesInvoiceResource, SalesInvoiceItemResource]
    export_form_class = SelectableFieldsExportForm

    custom_urls = (
        (
            "autocomplete/taxtype/",
            "invoices_taxtype_autocomplete",
            TaxTypeAutocompleteView.as_view(),
        ),
    )
    list_display = ("invoice_number", "get_status_label", "get_payment_status_label", "get_external_label", "company", "client", "date_issued", "total_amount")
    list_filter = (
        # Who / when
        ("company", AutocompleteSelectMultipleFilter),
        ("client", AutocompleteSelectMultipleFilter),
        ("date_issued", RangeDateFilter),
        # Document state
        ("status", ChoicesDropdownFilter),
        ("is_external", BooleanRadioFilter),
        # Payment
        ("payment_status", ChoicesDropdownFilter),
        DueDatePassedFilter,
        ("payment_method", ChoicesDropdownFilter),
        IsReconciledFilter,
    )
    search_fields = ("invoice_number", "client__name")
    autocomplete_fields = ("company", "client", "configuration", "project_file")
    inlines = [SalesInvoiceItemInline, AppliedTaxInline, PaymentAllocationInline, EmailLogInline, EInvoicingSubmissionInline]
    list_filter_submit = True
    list_select_related = ["company","client"]
    
    readonly_fields = ("incremental_id", "subtotal_amount", "tax_amount", "total_amount", "status", "invoice_number", "amount_paid", "amount_remaining", "is_external", "discount_amount", "discount_breakdown")

    def get_readonly_fields(self, request, obj=None):
        readonly_fields = super().get_readonly_fields(request, obj)
        if obj and obj.status == SalesInvoiceStatus.FINAL:
            all_fields = list(set(
                [f.name for f in self.model._meta.fields] +
                [f.name for f in self.model._meta.many_to_many] +
                list(readonly_fields)
            ))
            excluded_from_readonly = {"payment_status"}
            return [f for f in all_fields if f not in excluded_from_readonly]
        return readonly_fields

    def has_delete_permission(self, request, obj=None):
        if obj and obj.status == SalesInvoiceStatus.FINAL and not request.user.is_superuser:
            return False
        return super().has_delete_permission(request, obj)

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
                    "is_external",
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
                    ("discount_amount", "discount_breakdown"),
                    "tax_amount",
                    "total_amount",
                    "payment_method",
                    "payment_status",
                    "amount_paid",
                    "amount_remaining",
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

        mismatch = form.instance.get_balance_mismatch()
        if mismatch:
            calculated_total, manual_total = mismatch
            self.message_user(
                request,
                _(
                    "Warning: the calculated total (%(calculated)s) does not match "
                    "the invoice's recorded total (%(manual)s). Saved anyway — please "
                    "review the line items and taxes."
                ) % {"calculated": calculated_total, "manual": manual_total},
                level=messages.WARNING,
            )
    
    actions = ["download_pdf", "finalize_invoice_action", "mark_as_paid", "bulk_mark_as_paid", "import_from_excel","run_matcher_bulk"]
    actions_row = [
        "view_pdf_row_action",
        "download_pdf_row_action",
        "finalize_invoice_row_action",
        "send_email_row_action",
        "submit_aeat_row_action",
        "cancel_aeat_row_action",
        "view_journal_entry_row_action",
    ]
    actions_detail = [
        {
            "title": _("Invoice Actions"),
            "items": [
                "view_pdf_detail_action",
                "download_pdf_detail_action",
                "finalize_invoice_detail_action",
                "send_email_detail_action",
                "register_payment_detail_action",
                "run_invoice_matcher_detail_action",
            ],
        },
        {
            "title": _("AEAT / Verifactu"),
            "items": [
                "submit_aeat_detail_action",
                "cancel_aeat_detail_action",
            ],
        },
        {
            "title": _("Accounting"),
            "items": [
                "view_journal_entry_detail_action",
            ],
        },
    ]

    @action(description=_("View PDF"), icon="visibility", attrs={"target": "_blank"})
    def view_pdf_row_action(self, request, object_id):
        return redirect(reverse("admin:invoices_salesinvoice_pdf", args=[object_id]))

    @action(description=_("View PDF"), icon="visibility", attrs={"target": "_blank"})
    def view_pdf_detail_action(self, request, object_id):
        return redirect(reverse("admin:invoices_salesinvoice_pdf", args=[object_id]))

    @action(description=_("Download PDF"), icon="download")
    def download_pdf_row_action(self, request, object_id):
        return redirect(reverse("admin:invoices_salesinvoice_download_pdf", args=[object_id]))

    @action(description=_("Download PDF"), icon="download")
    def download_pdf_detail_action(self, request, object_id):
        return redirect(reverse("admin:invoices_salesinvoice_download_pdf", args=[object_id]))

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
            _(SalesInvoiceStatus.CANCELLED): "danger",
        },
    )
    def get_status_label(self, obj):
        return obj.status

    @display(
        description=_("Payment"),
        ordering="payment_status",
        label={
            _("Unpaid"): "danger",
            _("Partial"): "warning",
            _("Paid"): "success",
        },
    )
    def get_payment_status_label(self, obj):
        return obj.get_payment_status_display()

    @admin.action(description=_("Finalize selected pro forma invoices"))
    def finalize_invoice_action(self, request, queryset):
        count = 0
        for invoice in queryset.filter(status=SalesInvoiceStatus.PROFORMA):
            SalesInvoiceService.finalize_invoice(invoice)
            count += 1
        self.message_user(request, _("Finalized %d invoices and generated Delivery Orders.") % count)

    @admin.action(description=_("Mark selected invoices as Paid"))
    def mark_as_paid(self, request, queryset):
        count = queryset.update(payment_status=InvoicePaymentStatus.PAID)
        self.message_user(request, _("Marked %d invoices as paid.") % count)

    @admin.action(description=_("Download PDF"))
    def download_pdf(self, request, queryset):
        invoice_ids = list(queryset.values_list("pk", flat=True))
        if not invoice_ids:
            self.message_user(
                request,
                _("Select at least one invoice."),
                level=messages.ERROR,
            )
            return None

        if len(invoice_ids) == 1:
            invoice = queryset.select_related(
                "company", "client", "configuration"
            ).first()
            pdf_bytes = InvoicePdfDownloadService.render_sales_invoice_pdf(invoice)
            if not pdf_bytes:
                self.message_user(
                    request,
                    _("No PDF could be generated for the selected invoice."),
                    level=messages.ERROR,
                )
                return None
            return InvoicePdfDownloadService.pdf_attachment_response(
                pdf_bytes,
                InvoicePdfDownloadService.sales_invoice_pdf_filename(invoice),
            )

        result = build_invoice_pdf_zip_task.delay(
            "sales", invoice_ids, user_id=request.user.id
        )
        return redirect_to_pdf_zip_status(
            request,
            result.id,
            reverse("admin:invoices_salesinvoice_changelist"),
        )

    def get_urls(self):
        urls = super().get_urls()
        custom = [
            path(
                "add/",
                self.admin_site.admin_view(SalesInvoiceWizardView.as_view(model_admin=self)),
                name="invoices_salesinvoice_wizard",
            ),
            path(
                "import/",
                self.admin_site.admin_view(
                    SalesInvoiceImportAdminView.as_view(model_admin=self)
                ),
                name="invoices_salesinvoice_import",
            ),
            path(
                "bulk-mark-paid/",
                self.admin_site.admin_view(
                    BulkMarkPaidAdminView.as_view(model_admin=self)
                ),
                name="invoices_salesinvoice_bulk_mark_paid",
            ),
            path(
                "<int:object_id>/pdf/",
                self.admin_site.admin_view(SalesInvoicePdfView.as_view(model_admin=self)),
                name="invoices_salesinvoice_pdf",
            ),
            path(
                "<int:object_id>/download-pdf/",
                self.admin_site.admin_view(
                    SalesInvoicePdfDownloadView.as_view(model_admin=self)
                ),
                name="invoices_salesinvoice_download_pdf",
            ),
            path(
                "<int:object_id>/register-payment/",
                self.admin_site.admin_view(RegisterPaymentView.as_view(model_admin=self)),
                name="invoices_salesinvoice_register_payment",
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

    @action(description=_("Register Payment"), icon="payments")
    def register_payment_detail_action(self, request, object_id):
        return redirect(reverse("admin:invoices_salesinvoice_register_payment", args=[object_id]))

    @action(description=_("Send Email"), icon="mail")
    def send_email_row_action(self, request, object_id):
        invoice = SalesInvoice.objects.select_related("client").get(pk=object_id)
        if not invoice.client or not invoice.client.email:
            self.message_user(request, _("Client has no email address."), level="error")
        else:
            send_invoice_email_task.delay(invoice.id)
            self.message_user(request, _("Invoice email queued for %s.") % invoice.client.email)
        return redirect(reverse("admin:invoices_salesinvoice_changelist"))

    @action(description=_("Send Email"), icon="mail")
    def send_email_detail_action(self, request, object_id):
        invoice = SalesInvoice.objects.select_related("client").get(pk=object_id)
        if not invoice.client or not invoice.client.email:
            self.message_user(request, _("Client has no email address."), level="error")
        else:
            send_invoice_email_task.delay(invoice.id)
            self.message_user(request, _("Invoice email queued for %s.") % invoice.client.email)
        return redirect(reverse("admin:invoices_salesinvoice_change", args=[object_id]))

    @admin.action(
        description=_("Bulk update payment status"),
        permissions=["bulk_mark_paid_salesinvoice"],
    )
    def bulk_mark_as_paid(self, request, queryset):
        return redirect(reverse("admin:invoices_salesinvoice_bulk_mark_paid"))

    def has_bulk_mark_paid_salesinvoice_permission(self, request):
        return request.user.has_perm("invoices.bulk_mark_paid_salesinvoice")

    @admin.action(description=_("Import sales invoices from Excel"))
    def import_from_excel(self, request, queryset):
        return redirect(reverse("admin:invoices_salesinvoice_import"))

    @admin.action(description=_("Run bank matcher on selected invoices"))
    def run_matcher_bulk(self, request, queryset):
        from django.utils.html import format_html
        from django.utils.safestring import mark_safe

        ids = list(queryset.values_list("id", flat=True))
        for invoice_id in ids:
            run_invoice_matcher_task.delay("salesinvoice", invoice_id)
        suggestions_url = reverse("admin:banking_invoicereconciliationsuggestion_changelist") + "?is_confirmed=False"
        self.message_user(
            request,
            mark_safe(format_html(
                _("Queued bank matcher for {count} invoice(s). Results will appear in <a href=\"{url}\">Reconciliation Suggestions</a> shortly."),
                count=len(ids),
                url=suggestions_url,
            )),
        )

    @display(
        description=_("External"),
        label={
            _("External"): "info",
            _("Internal"): "default",
        },
    )
    def get_external_label(self, obj):
        if obj.is_external:
            return _("External")
        return _("Internal")

    @action(description=_("Submit to AEAT"), icon="cloud_upload")
    def submit_aeat_row_action(self, request, object_id):
        invoice = SalesInvoice.objects.get(pk=object_id)
        if invoice.status == SalesInvoiceStatus.CANCELLED:
            self.message_user(request, _("Cancelled invoices cannot be resubmitted."), level="error")
        elif invoice.status != SalesInvoiceStatus.FINAL:
            self.message_user(request, _("Only finalized invoices can be submitted to the AEAT."), level="error")
        else:
            submit_invoice_to_aeat.delay(invoice.id)
            self.message_user(request, _("AEAT submission queued for invoice %s.") % invoice.invoice_number)
        return redirect(reverse("admin:invoices_salesinvoice_changelist"))

    @action(description=_("Submit to AEAT"), icon="cloud_upload")
    def submit_aeat_detail_action(self, request, object_id):
        invoice = SalesInvoice.objects.get(pk=object_id)
        if invoice.status == SalesInvoiceStatus.CANCELLED:
            self.message_user(request, _("Cancelled invoices cannot be resubmitted."), level="error")
        elif invoice.status != SalesInvoiceStatus.FINAL:
            self.message_user(request, _("Only finalized invoices can be submitted to the AEAT."), level="error")
        else:
            submit_invoice_to_aeat.delay(invoice.id)
            self.message_user(request, _("AEAT submission queued for invoice %s.") % invoice.invoice_number)
        return redirect(reverse("admin:invoices_salesinvoice_change", args=[object_id]))

    @action(description=_("Cancel at AEAT"), icon="cancel")
    def cancel_aeat_row_action(self, request, object_id):
        invoice = SalesInvoice.objects.get(pk=object_id)
        if invoice.status == SalesInvoiceStatus.CANCELLED:
            self.message_user(request, _("Invoice is already cancelled."), level="warning")
        else:
            cancel_invoice_at_aeat.delay(invoice.id)
            self.message_user(request, _("AEAT cancellation queued for invoice %s.") % (invoice.invoice_number or invoice.pk))
        return redirect(reverse("admin:invoices_salesinvoice_changelist"))

    @action(description=_("Cancel at AEAT"), icon="cancel")
    def cancel_aeat_detail_action(self, request, object_id):
        invoice = SalesInvoice.objects.get(pk=object_id)
        if invoice.status == SalesInvoiceStatus.CANCELLED:
            self.message_user(request, _("Invoice is already cancelled."), level="warning")
        else:
            cancel_invoice_at_aeat.delay(invoice.id)
            self.message_user(request, _("AEAT cancellation queued for invoice %s.") % (invoice.invoice_number or invoice.pk))
        return redirect(reverse("admin:invoices_salesinvoice_change", args=[object_id]))

    @action(description=_("Run Matcher"), url_path="run-matcher", icon="compare_arrows")
    def run_invoice_matcher_detail_action(self, request, object_id):
        from banking.tasks import run_invoice_matcher_task

        invoice = get_object_or_404(SalesInvoice, pk=object_id)

        # Dispatch to Celery and redirect to the status page
        result = run_invoice_matcher_task.delay("salesinvoice", invoice.pk)
        back_url = reverse("admin:invoices_salesinvoice_change", args=[invoice.pk])
        status_url = (
            reverse("admin:banking_matcher_status")
            + f"?task_id={result.id}&task_type=salesinvoice&back_url={back_url}"
        )
        return redirect(status_url)