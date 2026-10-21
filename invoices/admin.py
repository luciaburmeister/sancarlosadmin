from unfold.contrib.filters.admin import MultipleChoicesDropdownFilter
from unfold.contrib.filters.admin import RangeNumericFilter
from unfold.contrib.filters.admin import BooleanRadioFilter
import logging
from decimal import Decimal

from assets import urls
from unfold.admin import StackedInline
from django.contrib import admin
from django.contrib import messages
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect
from unfold.decorators import action, display
from django.urls import path, reverse
from django import forms
from django.core.exceptions import ValidationError
from core.forms.select import StaticSelect2MultipleWidget
from materials.models import PaymentMethod

logger = logging.getLogger(__name__)
from django.utils.translation import gettext_lazy as _
from datetime import timedelta
from unfold.admin import ModelAdmin, GenericTabularInline, TabularInline
from unfold.contrib.forms.widgets import ArrayWidget
from django.contrib.postgres.fields import ArrayField
from django.utils.formats import number_format
from django.utils.html import format_html, format_html_join
from django.db import models, transaction
from django.db.models import OuterRef, Subquery, Sum
from django.db.models.functions import Coalesce
from invoices.views.invoice.aging_export_view import AgingExportView
from general.djangoql.admin import DjangoQLSearchAdminMixin

from invoices.models import (
    CollectionsForecastEmailSettings,
    Invoice,
    ConfirmationStatus,
    TaxType,
    AppliedTax,
    IncomingInvoice,
    InvoicePaymentStatus,
    SalesInvoice,
    SalesInvoiceDocumentType,
    SalesInvoiceItem,
    SalesInvoiceStatus,
    InvoiceItem,
    InvoicePOAllocation,
    PaymentReminderSettings,
    PaymentReminderRule,
    PaymentReminderLog,
    PaymentReminderLogStatus,
    PaymentsForecastEmailSettings,
)
from invoices.views.invoice.aging_export_view import AgingExportView
from payments.admin_inlines import DocumentPaymentsInline
from django.utils import timezone
from invoices.services.invoice_pdf_download import InvoicePdfDownloadService
from invoices.tasks import build_invoice_pdf_zip_task
from core.documents import enqueue_document, redirect_to_document_status
from invoices.documents import render_sales_invoice_pdf
from invoices.views.invoice.wizard_views import SalesInvoiceWizardView
from invoices.views.invoice.send_reminder_now import SendPaymentReminderNowAdminView
from invoices.views.invoice.download_invoice_file import PurchaseInvoiceFileDownloadView
from invoices.views.sales_invoice_pdf import SalesInvoicePdfDownloadView, SalesInvoicePdfView
from invoices.views.invoice.import_sales_invoice import SalesInvoiceImportAdminView
from invoices.views.invoice.bulk_mark_paid import BulkMarkPaidAdminView
from invoices.views.invoice.bulk_mark_invoice_paid import BulkMarkInvoicePaidAdminView
from invoices.views.invoice.autocomplete_views import TaxTypeAutocompleteView
from invoices.views.sales_invoice_register_payment import RegisterPaymentView, SalesInvoiceDistributeSurplusView
from invoices.views.sales_invoice_apply_credit import SalesInvoiceApplyCreditView
from invoices.views.invoice.review_invoice import InvoiceReviewAdminView
from invoices.views.invoice.match_po import InvoiceMatchPOView
from invoices.views.invoice.ai_match_view import InvoiceAIMatchView, InvoiceToggleSkipPOMatchingView
from invoices.views.invoice.upload_invoice import InvoiceUploadAdminView
from invoices.views.invoice.import_invoice import InvoiceImportAdminView
from invoices.views.invoice_register_payment import InvoiceRegisterPaymentView, InvoiceDistributeSurplusView
from invoices.views.invoice_apply_credit import InvoiceApplyCreditView
from invoices.views.accounts_payable_views import AccountsPayableDashboardView
from invoices.views.operational_dashboard_view import OperationalDashboardView
from invoices.views.invoice.collections_forecast_view import (
    CollectionsForecastSetCommitmentDateView,
    CollectionsForecastView,
)
from invoices.views.invoice.payments_forecast_view import PaymentsForecastView
from invoices.services.sales_invoice_service import SalesInvoiceService
from invoices.services.advances import AdvanceDeductionService, AdvanceInvoiceService
from invoices.services.project_file_invoice_service import ProjectFileInvoiceService
from invoices.services.discounts import (
    ADVANCE_DEDUCTION_PREFIX,
    ADVANCE_LINE_PREFIX,
    is_advance_deduction_source,
    is_advance_line_source,
)
from apps.einvoicing.admin import EInvoicingSubmissionInline
from apps.einvoicing.models import EInvoicingAccount
from apps.einvoicing.services import EInvoicingService
from apps.einvoicing.tasks import submit_invoice_to_aeat, cancel_invoice_at_aeat
from invoices.tasks import process_incoming_invoices, process_manual_invoice
from banking.tasks import run_invoice_matcher_task
from accounting.tasks import classify_invoice_accounts, generate_journal_entries_task, export_a3_purchase_invoicing_task
from core.tasks import send_invoice_email_task
from general.email import sender_cc, sender_reply_to
from company.models import EmailLog
from core.admin.company_admin import CompanyRestrictedAdmin
from simple_history.admin import SimpleHistoryAdmin
from unfold.contrib.filters.admin import (
    RangeDateFilter,
    ChoicesDropdownFilter,
    AutocompleteSelectMultipleFilter
)
from unfold.widgets import UnfoldAdminTextareaWidget, SELECT_CLASSES
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

# Issued and cancelled sales invoices: their lines and taxes cannot change
# (SalesInvoiceItem.save / AppliedTax.save refuse it too).
FROZEN_SALES_INVOICE_STATUSES = (SalesInvoiceStatus.FINAL, SalesInvoiceStatus.CANCELLED)


def _has_advance_lines(invoice):
    """Whether the sales invoice is an advance or deducts one: its tax bases
    are then kept by the advance services."""
    # Asked several times per change page (fields, fieldsets, inlines)
    if not hasattr(invoice, "_has_advance_lines"):
        invoice._has_advance_lines = invoice.items.filter(
            models.Q(source_id__startswith=ADVANCE_LINE_PREFIX)
            | models.Q(source_id__startswith=ADVANCE_DEDUCTION_PREFIX)
        ).exists()
    return invoice._has_advance_lines


def _is_advance_related(invoice):
    """An advance invoice, or an invoice that deducts one."""
    return invoice.document_type == SalesInvoiceDocumentType.ADVANCE or _has_advance_lines(invoice)


# An advance and the invoices deducting it keep the header the deduction was
# matched on (AdvanceDeductionService only deducts within one expediente,
# company, client and currency).
ADVANCE_LOCKED_FIELDS = ("project_file", "company", "client", "currency")


def _sales_invoice_link(invoice):
    return format_html(
        '<a href="{}">{}</a>',
        reverse("admin:invoices_salesinvoice_change", args=[invoice.pk]),
        invoice.display_number,
    )


def _pill(text, color):
    return format_html(
        '<span style="display:inline-block;padding:1px 8px;border-radius:999px;'
        'background:{};color:white;font-size:11px;font-weight:600;">{}</span>',
        color, text,
    )


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

    def get_readonly_fields(self, request, obj=None):
        readonly_fields = super().get_readonly_fields(request, obj)
        # A deduction lowered these bases by its amount; only the pro
        # forma's advance actions move them.
        if isinstance(obj, SalesInvoice) and obj.pk and _has_advance_lines(obj):
            return (*readonly_fields, "base_amount")
        return readonly_fields

    @staticmethod
    def _kept_elsewhere(obj):
        """Frozen invoices, and advances: an advance's taxes mirror its
        lines' taxes (AdvanceInvoiceService); to change one, delete the pro
        forma and issue it again."""
        return isinstance(obj, SalesInvoice) and (
            obj.status in FROZEN_SALES_INVOICE_STATUSES
            or obj.document_type == SalesInvoiceDocumentType.ADVANCE
        )

    def has_add_permission(self, request, obj):
        if self._kept_elsewhere(obj):
            return False
        return super().has_add_permission(request, obj)

    def has_change_permission(self, request, obj=None):
        if self._kept_elsewhere(obj):
            return False
        return super().has_change_permission(request, obj)

    def has_delete_permission(self, request, obj=None):
        if self._kept_elsewhere(obj):
            return False
        return super().has_delete_permission(request, obj)

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
                    ("account", "is_rounding"),
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


class LineItemExportMixin:
    """
    Export resources registered alongside the header resource may target a
    line-item model (e.g. InvoiceItem, SalesInvoiceItem) instead of this
    admin's own model. django-import-export always builds the export
    queryset from the admin's model, so when an item resource is chosen we
    need to swap in the matching item queryset via its `invoice` FK.
    """

    def get_data_for_export(self, request, queryset, **kwargs):
        export_form = kwargs.get("export_form")
        export_class = self.choose_export_resource_class(export_form, request)
        if export_class._meta.model is not self.model:
            queryset = export_class._meta.model.objects.filter(invoice__in=queryset)
        return super().get_data_for_export(request, queryset, **kwargs)


@admin.register(Invoice)
class InvoiceAdmin(LineItemExportMixin, DjangoQLSearchAdminMixin, SimpleHistoryAdmin, ModelAdmin, CompanyRestrictedAdmin, ExportActionModelAdmin):
    resource_classes = [InvoiceResource, InvoiceItemResource]
    import_form_class = ImportForm
    export_form_class = SelectableFieldsExportForm

    list_display = (
        "invoice_number",
        "provider",
        "total_amount",
        "pending_amount_display",
        "date_issued",
        "due_date",
        "created_at",
        "ocr_status",
        "balance_status",
        "get_payment_status_label",
        "confirmation_badge",
        "po_match_status_badge",
    )

    list_filter = (
        # Who / when
        ("company", AutocompleteSelectMultipleFilter),
        ("provider", AutocompleteSelectMultipleFilter),
        ("date_issued", RangeDateFilter),
        ("total_amount",RangeNumericFilter),
        # Document state
        ("document_type", ChoicesDropdownFilter),
        ("confirmation_status", ChoicesDropdownFilter),
        # Payment
        ("payment_status", ChoicesDropdownFilter),
        DueDatePassedFilter,
        ("payment_method", ChoicesDropdownFilter),
        IsReconciledFilter,
        # Accounting diagnostics
        BalanceStatusFilter,
        ItemsMissingAccountFilter,
        ("cost_center", AutocompleteSelectMultipleFilter),
    )

    search_fields = (
        "invoice_number",
        "provider__name",
    )

    autocomplete_fields = (
        "provider",
        "account",
        "project_file",
        "cost_center",
        "rectifies",
    )

    readonly_fields = (
        "net_amount",
        "total_amount",
        "gemini_account_response",
        "ocr_raw_response",
        "ocr_error",
        "amount_paid",
        "amount_remaining",
        "get_linked_fixed_cost_installment",
    )

    list_select_related = (
        "provider",
        "company",
        "cost_center",
    )

    date_hierarchy = "date_issued"
    
    list_filter_submit = True

    inlines = [
        AppliedTaxInline,
        InvoiceItemInline,
        InvoicePOAllocationInline,
        DocumentPaymentsInline,
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
                "apply_credit_detail_action",
                "run_invoice_matcher_detail_action",
                "match_po_detail_action",
                "ai_match_detail_action",
            ],
        },
        {
            "title": _("Accounting"),
            "items": [
                "view_journal_entry_detail_action",
            ],
        },
    ]

    def get_queryset(self, request):
        from payments.mixins import annotate_amount_remaining, annotate_effective_date

        qs = super().get_queryset(request)
        qs = annotate_effective_date(qs)
        qs = annotate_amount_remaining(qs, Invoice)
        # Independent Subquery annotations — avoids the Django fan-out bug
        # that a single Sum("items__po_allocations__quantity") would hit
        # when an item has multiple allocations. Powers po_match_status_badge.
        total_qty_sq = (
            InvoiceItem.objects.filter(invoice=OuterRef("pk"))
            .values("invoice")
            .annotate(total=Sum("quantity"))
            .values("total")
        )
        matched_qty_sq = (
            InvoicePOAllocation.objects.filter(invoice_item__invoice=OuterRef("pk"))
            .values("invoice_item__invoice")
            .annotate(total=Sum("quantity"))
            .values("total")
        )
        return qs.annotate(
            _total_item_qty=Subquery(total_qty_sq),
            _matched_item_qty=Subquery(matched_qty_sq),
        )

    def lookup_allowed(self, lookup, value):
        if lookup.split("__")[0] == "effective_date":
            return True
        return super().lookup_allowed(lookup, value)

    @display(
        description=_("PO Match"),
        label={
            "Matched": "success",
            "Partial": "warning",
            "Unmatched": "danger",
            "No relevant PO": "default",
        },
    )
    def po_match_status_badge(self, obj):
        if obj.skip_po_matching:
            return "No relevant PO"

        total = obj._total_item_qty or Decimal("0")
        matched = obj._matched_item_qty or Decimal("0")

        if total <= 0:
            return "Unmatched"
        if matched >= total:
            return "Matched"
        if matched > 0:
            return "Partial"
        return "Unmatched"

    @action(description=_("Match Purchase Order"), icon="link")
    def match_po_detail_action(self, request, object_id):
        return redirect(reverse("admin:invoices_invoice_match_po", args=[object_id]))

    @action(description=_("AI Suggest Match"), icon="auto_awesome")
    def ai_match_detail_action(self, request, object_id):
        return self._dispatch_ai_match(request, object_id)

    def _dispatch_ai_match(self, request, object_id):
        """
        Shared dispatch for the AI matcher: queues suggest_ai_po_matches_task
        and redirects to the polling status page. Called both from this
        admin action and from the "Try AI Suggest Match" link inside
        match_po.html (invoices_invoice_run_ai_match URL below) — both
        entry points must actually run the matcher, not just navigate to
        the (possibly empty) review screen.
        """
        from invoices.tasks import suggest_ai_po_matches_task

        invoice = get_object_or_404(Invoice, pk=object_id)

        if invoice.skip_po_matching:
            messages.warning(
                request,
                _("This invoice is marked as having no relevant purchase order."),
            )
            return redirect(reverse("admin:invoices_invoice_change", args=[invoice.pk]))

        result = suggest_ai_po_matches_task.delay(invoice.pk)
        back_url = reverse("admin:invoices_invoice_ai_match", args=[invoice.pk])
        status_url = (
            reverse("admin:banking_matcher_status")
            + f"?task_id={result.id}&task_type=ai_po_match&back_url={back_url}"
        )
        return redirect(status_url)

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
        return redirect_to_document_status(
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
                    ("document_type", "rectifies"),
                    ("net_amount", "total_amount"),
                    ("currency", "company"),
                    ("project_file", "cost_center"),
                    "file",
                )
            },
        ),
        (
            _("Dates"),
            {
                "fields": (
                    ("date_issued", "due_date"),
                    "accounting_date",
                    "commitment_date",
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
                    "skip_po_matching",
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
                    "get_linked_fixed_cost_installment",
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
                "dashboard/payments-forecast/",
                self.admin_site.admin_view(
                    PaymentsForecastView.as_view(model_admin=self)
                ),
                name="invoices_invoice_payments_forecast",
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
                "<int:object_id>/register-payment/distribute-surplus/",
                self.admin_site.admin_view(
                    InvoiceDistributeSurplusView.as_view(model_admin=self)
                ),
                name="invoices_invoice_distribute_surplus",
            ),
            path(
                "<int:object_id>/apply-credit/",
                self.admin_site.admin_view(
                    InvoiceApplyCreditView.as_view(model_admin=self)
                ),
                name="invoices_invoice_apply_credit",
            ),
            path(
                "<int:object_id>/match-po/",
                self.admin_site.admin_view(
                    InvoiceMatchPOView.as_view(model_admin=self)
                ),
                name="invoices_invoice_match_po",
            ),
            path(
                "<int:object_id>/ai-match/",
                self.admin_site.admin_view(
                    InvoiceAIMatchView.as_view(model_admin=self)
                ),
                name="invoices_invoice_ai_match",
            ),
            path(
                "<int:object_id>/run-ai-match/",
                self.admin_site.admin_view(self._dispatch_ai_match),
                name="invoices_invoice_run_ai_match",
            ),
            path(
                "<int:object_id>/toggle-skip-po-matching/",
                self.admin_site.admin_view(
                    InvoiceToggleSkipPOMatchingView.as_view(model_admin=self)
                ),
                name="invoices_invoice_toggle_skip_po_matching",
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
            True: "success",  # green
            False: "danger",  # red
        },)
    def balance_status(self, obj):
        balanced = bool(obj.is_total_balanced)
        return balanced, _("Balanced") if balanced else _("Unbalanced")
        
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
            InvoicePaymentStatus.UNPAID: "danger",
            InvoicePaymentStatus.PARTIAL: "warning",
            InvoicePaymentStatus.PAID: "success",
        },
    )
    def get_payment_status_label(self, obj):
        return obj.payment_status, obj.get_payment_status_display()

    @admin.display(description=_("Linked Fixed Cost Installment"))
    def get_linked_fixed_cost_installment(self, obj):
        from django.apps import apps
        from django.urls import reverse
        from django.utils.html import format_html

        FixedCostInstallment = apps.get_model("expenses", "FixedCostInstallment")
        installment = FixedCostInstallment.objects.filter(matched_invoice=obj).select_related("schedule").first()
        if not installment:
            return "—"
        url = reverse("admin:expenses_fixedcostinstallment_change", args=[installment.pk])
        return format_html('<a href="{}" class="text-primary-600 underline">{}</a>', url, str(installment))

    @display(description=_("Pending Amount"), ordering="db_amount_remaining")
    def pending_amount_display(self, obj):
        from payments.mixins import currency_symbol

        amount = getattr(obj, "db_amount_remaining", None)
        if amount is None:
            return "—"
        return f"{amount} {currency_symbol(obj.total_amount_currency)}"

    @action(description=_("Register Payment"), icon="payments")
    def register_payment_detail_action(self, request, object_id):
        return redirect(reverse("admin:invoices_invoice_register_payment", args=[object_id]))

    def has_apply_credit_permission(self, request, object_id=None):
        if not object_id:
            return True
        invoice = Invoice.objects.filter(pk=object_id).first()
        if not invoice:
            return False
        return InvoiceApplyCreditView().can_apply_credit(request, invoice)

    @action(description=_("Apply Credit"), icon="account_balance_wallet", permissions=["apply_credit"])
    def apply_credit_detail_action(self, request, object_id):
        return redirect(reverse("admin:invoices_invoice_apply_credit", args=[object_id]))

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
        end_date = timezone.localdate()
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

class SalesInvoiceItemInlineFormSet(forms.BaseInlineFormSet):
    """Advance lines and advance deductions are kept by the advance services
    (their amounts are in the tax bases): they cannot be edited or deleted
    here."""

    def clean(self):
        super().clean()
        for form in self.forms:
            line = form.instance
            if not line.pk or not (
                is_advance_line_source(line.source_id) or is_advance_deduction_source(line.source_id)
            ):
                continue
            changed = set(form.changed_data)
            if is_advance_line_source(line.source_id):
                changed.discard("business_unit")  # what the advance sells is still the user's call
            if self._should_delete_form(form) or changed:
                raise ValidationError(_(
                    "Advance lines and advance deductions cannot be edited or deleted here. "
                    "Deductions are changed with the pro forma's Remove advances and "
                    "Reapply advances actions."
                ))


class SalesInvoiceItemInline(StackedInline):
    model = SalesInvoiceItem
    formset = SalesInvoiceItemInlineFormSet
    extra = 0
    tab = True
    readonly_fields = ("discount_amount", "line_total", "source_id", "project_file")
    autocomplete_fields = ("business_unit",)
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
                    ("project_file", "business_unit"),
                )
            },
        ),
    )

    def has_add_permission(self, request, obj):
        if obj and obj.status in FROZEN_SALES_INVOICE_STATUSES:
            return False
        # An advance's lines are its VAT groups (AdvanceInvoiceService): a line
        # added here would be billed untaxed and deducted as a group of its own.
        if obj and obj.document_type == SalesInvoiceDocumentType.ADVANCE:
            return False
        return super().has_add_permission(request, obj)

    def has_change_permission(self, request, obj=None):
        if obj and obj.status in FROZEN_SALES_INVOICE_STATUSES:
            return False
        return super().has_change_permission(request, obj)

    def has_delete_permission(self, request, obj=None):
        if obj and obj.status in FROZEN_SALES_INVOICE_STATUSES:
            return False
        return super().has_delete_permission(request, obj)

class SalesInvoiceBusinessUnitFormSet(forms.BaseInlineFormSet):
    def save_existing(self, form, obj, commit=True):
        # The line itself is frozen: only its business unit is saved
        if commit:
            obj.save(update_fields=["business_unit"])
        return obj


class SalesInvoiceBusinessUnitInline(TabularInline):
    """An issued invoice's lines, to correct their business unit until its
    journal entry is posted (SalesInvoiceItem.save allows only that)."""

    model = SalesInvoiceItem
    formset = SalesInvoiceBusinessUnitFormSet
    extra = 0
    tab = True
    verbose_name = _("Business unit")
    verbose_name_plural = _("Business units")
    fields = ("description", "line_total", "business_unit")
    autocomplete_fields = ("business_unit",)
    can_delete = False

    def get_readonly_fields(self, request, obj=None):
        if obj and obj.has_posted_journal_entry:
            return self.fields
        return ("description", "line_total")

    def has_add_permission(self, request, obj):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


class EmailLogInline(GenericTabularInline):
    model = EmailLog
    extra = 0
    tab = True
    verbose_name = _("Email Sent")
    verbose_name_plural = _("Emails Sent")
    fields = (
        "to_email", "subject", "sent_at", "status", "delivery_status",
        "delivered_at", "first_opened_at", "last_opened_at", "open_count",
        "delivery_error", "error_message",
    )
    readonly_fields = fields
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False

class PaymentReminderLogInline(TabularInline):
    model = PaymentReminderLog
    extra = 0
    tab = True
    verbose_name = _("Payment Reminder")
    verbose_name_plural = _("Payment Reminders")
    fields = ("reminder_type", "rule", "scheduled_for", "status", "sent_at", "error_message")
    readonly_fields = ("reminder_type", "rule", "scheduled_for", "status", "sent_at", "error_message")
    can_delete = False
    ordering = ("-scheduled_for",)

    def has_add_permission(self, request, obj=None):
        return False

@admin.register(SalesInvoice)
class SalesInvoiceAdmin(LineItemExportMixin, DjangoQLSearchAdminMixin, SimpleHistoryAdmin, ModelAdmin, CompanyRestrictedAdmin, ExportActionModelAdmin):
    resource_classes = [SalesInvoiceResource, SalesInvoiceItemResource]
    export_form_class = SelectableFieldsExportForm

    custom_urls = (
        (
            "autocomplete/taxtype/",
            "invoices_taxtype_autocomplete",
            TaxTypeAutocompleteView.as_view(),
        ),
    )
    list_display = (
        "get_document_number",
        "get_document_type_label",
        "client",
        "total_amount",
        "date_issued",
        "get_payment_status_label",
        "due_date",
        "payment_commitment_date",
        "get_status_label",
        "get_external_label",
        "company",
    )
    list_filter = (
        ("company", AutocompleteSelectMultipleFilter),
        ("client", AutocompleteSelectMultipleFilter),
        ("date_issued", RangeDateFilter),
        ("due_date", RangeDateFilter),
        ("payment_commitment_date", RangeDateFilter),
        ("total_amount",RangeNumericFilter),
        # Document state
        ("status", MultipleChoicesDropdownFilter),
        ("document_type", ChoicesDropdownFilter),
        ("is_external", BooleanRadioFilter),
        # Payment
        ("payment_status", MultipleChoicesDropdownFilter),
        DueDatePassedFilter,
        ("payment_method", MultipleChoicesDropdownFilter),
        IsReconciledFilter,
    )
    search_fields = ("invoice_number", "proforma_number", "client__name")
    autocomplete_fields = ("company", "client", "configuration", "project_file")
    inlines = [SalesInvoiceItemInline, AppliedTaxInline, DocumentPaymentsInline, EmailLogInline, EInvoicingSubmissionInline, PaymentReminderLogInline]

    def get_inlines(self, request, obj):
        inlines = list(super().get_inlines(request, obj))
        if obj and obj.status == SalesInvoiceStatus.FINAL:
            inlines.insert(1, SalesInvoiceBusinessUnitInline)
        return inlines
    list_filter_submit = True
    list_select_related = ["company","client"]
    
    readonly_fields = ("incremental_id", "subtotal_amount", "lines_total_display", "taxable_base_display", "tax_amount", "total_amount", "status", "invoice_number", "proforma_number", "amount_paid", "amount_remaining", "is_external", "discount_amount", "discount_breakdown", "get_late_commitment_warning", "advances_display")

    def get_queryset(self, request):
        from payments.mixins import annotate_amount_remaining, annotate_forecast_date

        qs = super().get_queryset(request)
        qs = annotate_forecast_date(qs)
        return annotate_amount_remaining(qs, SalesInvoice)

    def lookup_allowed(self, lookup, value):
        if lookup.split("__")[0] == "forecast_date":
            return True
        return super().lookup_allowed(lookup, value)

    @display(description=_("Lines total (before discount)"))
    def lines_total_display(self, obj):
        return obj.subtotal_amount

    @display(description=_("Subtotal"))
    def taxable_base_display(self, obj):
        return obj.taxable_base

    def get_readonly_fields(self, request, obj=None):
        readonly_fields = super().get_readonly_fields(request, obj)
        # Pro formas included: the deduction was matched on these fields
        locked = obj is not None and obj.pk is not None and _is_advance_related(obj)
        if obj and obj.status == SalesInvoiceStatus.FINAL:
            all_fields = list(set(
                [f.name for f in self.model._meta.fields] +
                [f.name for f in self.model._meta.many_to_many] +
                list(readonly_fields)
            ))
            excluded_from_readonly = {"payment_status", "exclude_from_payment_reminders", "payment_commitment_date","project_file"}
            if locked:
                excluded_from_readonly -= set(ADVANCE_LOCKED_FIELDS)
            return [f for f in all_fields if f not in excluded_from_readonly]
        if locked:
            return [*readonly_fields, *(f for f in ADVANCE_LOCKED_FIELDS if f not in readonly_fields)]
        return readonly_fields

    def get_fieldsets(self, request, obj=None):
        fieldsets = super().get_fieldsets(request, obj)
        if obj is not None and obj.pk is not None and _is_advance_related(obj):
            general, *rest = fieldsets
            return (general, (_("Advances"), {"fields": ("advances_display",)}), *rest)
        return fieldsets

    def has_delete_permission(self, request, obj=None):
        if obj and obj.status == SalesInvoiceStatus.FINAL and not request.user.is_superuser:
            return False
        return super().has_delete_permission(request, obj)

    @display(description=_("Deductions"))
    def advances_display(self, obj):
        """An advance: where it has been deducted and what is left to deduct.
        Any other invoice: the advances it deducts."""
        if not obj.pk:
            return "—"
        if obj.document_type == SalesInvoiceDocumentType.ADVANCE:
            return self._advance_panel(obj)
        return self._deductions_panel(obj)

    def _advance_panel(self, invoice):
        [advance] = ProjectFileInvoiceService._advances_data([invoice])
        # Deductions on cancelled invoices give the advance back
        deducted_in = {}
        for line in (
            SalesInvoiceItem.objects.filter(advance_line__invoice=invoice)
            .exclude(invoice__status=SalesInvoiceStatus.CANCELLED)
            .select_related("invoice")
            .order_by("invoice_id", "pk")
        ):
            deducted_in[line.invoice] = deducted_in.get(line.invoice, Decimal("0.00")) + line.line_total.amount

        badges = []
        if invoice.status == SalesInvoiceStatus.PROFORMA:
            badges.append(_pill(_("pro forma: issue it as final to deduct it"), "#d97706"))
        elif invoice.status == SalesInvoiceStatus.FINAL:
            if not advance["paid"]:
                badges.append(_pill(_("Unpaid"), "#dc2626"))
            if not advance["deductible"]:
                badges.append(_pill(_("not deductible"), "#6b7280"))
        else:
            badges.append(_pill(invoice.get_status_display(), "#6b7280"))

        percentage = (
            f"{AdvanceInvoiceService._format_percentage(invoice.advance_percentage)} %"
            if invoice.advance_percentage is not None else "—"
        )
        rows = [
            (_("Advance (%)"), percentage, format_html_join(" ", "{}", ((b,) for b in badges))),
            (_("Base"), number_format(advance["base"], 2), ""),
        ]
        for deducting, amount in deducted_in.items():
            label = format_html(_("Deducted in {invoice}"), invoice=_sales_invoice_link(deducting))
            if deducting.status != SalesInvoiceStatus.FINAL:
                label = format_html("{} ({})", label, deducting.get_status_display())
            rows.append((label, number_format(amount, 2), ""))
        if invoice.status != SalesInvoiceStatus.CANCELLED:
            rows.append((_("Pending"), number_format(advance["pending"], 2), ""))
        return self._advance_rows_table(rows)

    def _deductions_panel(self, invoice):
        deducted = {}  # advance invoice → what this invoice deducts of it (≤ 0)
        for line in AdvanceDeductionService.deduction_lines(invoice):
            advance = line.advance_line.invoice
            deducted[advance] = deducted.get(advance, Decimal("0.00")) + line.line_total.amount
        if not deducted:
            return "—"

        rows = []
        for advance, amount in deducted.items():
            label = format_html(
                "{} · {}", _sales_invoice_link(advance), advance.date_issued.strftime("%d/%m/%Y"),
            )
            if advance.advance_percentage is not None:
                label = format_html(
                    "{} ({} %)", label, AdvanceInvoiceService._format_percentage(advance.advance_percentage),
                )
            # Deductible once final, paid or not: say when it is not paid yet
            if advance.status != SalesInvoiceStatus.FINAL:
                badge = _pill(advance.get_status_display(), "#6b7280")
            elif advance.payment_status != InvoicePaymentStatus.PAID:
                badge = _pill(_("Unpaid"), "#dc2626")
            else:
                badge = ""
            rows.append((label, number_format(amount, 2), badge))
        return self._advance_rows_table(rows)

    @staticmethod
    def _advance_rows_table(rows):
        """``rows`` of (label, amount, badges) as a small table."""
        return format_html(
            '<table style="width: 100%; max-width: 640px; font-size: 13px; color: #374151;">{}</table>',
            format_html_join(
                "",
                '<tr style="border-top: 1px solid #e5e7eb;">'
                '<td style="padding: 4px 6px 4px 0;">{}</td>'
                '<td style="padding: 4px 6px; text-align: right; white-space: nowrap;">{}</td>'
                '<td style="padding: 4px 0 4px 6px;">{}</td>'
                '</tr>',
                rows,
            ),
        )

    fieldsets = (
        (
            _("General Information"),
            {
                "fields": (
                    ("company", "client"),
                    ("status", "invoice_number", "proforma_number"),
                    ("date_issued", "due_date"),
                    ("payment_commitment_date", "get_late_commitment_warning"),
                    "project_file",
                    ("reference"),
                    ("is_external", "exclude_from_payment_reminders"),
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
                    "currency",
                    "lines_total_display",
                    ("discount_amount", "discount_breakdown"),
                    "taxable_base_display",
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

    @action(description=_("Send Payment Reminder Now"), icon="mail")
    def send_reminder_now_detail_action(self, request, object_id):
        return redirect(reverse("admin:invoices_salesinvoice_send_reminder_now", args=[object_id]))

    def save_model(self, request, obj, form, change):
        if change:
            # Its expedientes before its row, as every invoicing path locks
            # them: saving the invoice updates their totals
            ProjectFileInvoiceService.lock_project_files(
                ProjectFileInvoiceService.project_file_ids_of(obj)
            )
        super().save_model(request, obj, form, change)

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
    
    actions = ["download_pdf", "finalize_invoice_action", "mark_as_paid", "bulk_mark_as_paid", "import_from_excel","run_matcher_bulk", "journalize_selected"]
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
                "apply_credit_detail_action",
                "run_invoice_matcher_detail_action",
                "remove_advances_detail_action",
                "reapply_advances_detail_action",
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
                "journalize_detail_action",
            ],
        },
        {
            "title": _("Payment Reminders"),
            "items": [
                "send_reminder_now_detail_action",
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

    def _finalize_with_message(self, request, object_id):
        invoice = get_object_or_404(self.get_queryset(request), pk=object_id)
        if invoice.status == SalesInvoiceStatus.FINAL:
            self.message_user(request, _("Invoice is already final."), level="warning")
            return
        try:
            SalesInvoiceService.finalize_invoice(
                invoice,
                reply_to=sender_reply_to(request.user),
                cc_emails=sender_cc(request.user),
            )
        except ValidationError as e:
            self.message_user(request, " ".join(e.messages), level=messages.ERROR)
        else:
            self.message_user(request, _("Invoice %s finalized.") % invoice.invoice_number)

    # Finalize, Cancel at AEAT, Remove and Reapply advances change the
    # invoice: behind a dialog, so they only run on its CSRF-protected POST
    # and never on a plain link.
    _FINALIZE_DIALOG = {
        "title": _("Finalize"),
        "description": _(
            "Issues this pro forma with the next invoice number and emails it "
            "to the client. An issued invoice can no longer be changed."
        ),
        "form_submit_text": _("Finalize"),
    }

    @action(
        description=_("Finalize"), icon="gavel",
        permissions=["change_salesinvoice"], dialog=_FINALIZE_DIALOG,
    )
    def finalize_invoice_row_action(self, request, form, object_id):
        self._finalize_with_message(request, object_id)
        return self._dialog_redirect()

    @action(
        description=_("Finalize"), icon="gavel",
        permissions=["change_salesinvoice"], dialog=_FINALIZE_DIALOG,
    )
    def finalize_invoice_detail_action(self, request, form, object_id):
        self._finalize_with_message(request, object_id)
        return self._dialog_redirect(object_id)

    @staticmethod
    def _dialog_redirect(object_id=None):
        """Back to the invoice, or to the list after a row action (no
        ``object_id``), after a dialog action. The dialog posts with htmx,
        which would follow a real redirect inside the modal: see
        ExpenseClaimAdmin.reject_claim_detail_action."""
        response = HttpResponse(status=200)
        if object_id is None:
            response["HX-Redirect"] = reverse("admin:invoices_salesinvoice_changelist")
        else:
            response["HX-Redirect"] = reverse("admin:invoices_salesinvoice_change", args=[object_id])
        return response

    @action(
        description=_("Remove advances"), icon="remove_circle",
        permissions=["change_salesinvoice"],
        dialog={
            "title": _("Remove advances"),
            "description": _(
                "Removes this pro forma's advance deductions. "
                "Reapply advances deducts them again."
            ),
            "form_submit_text": _("Remove advances"),
        },
    )
    def remove_advances_detail_action(self, request, form, object_id):
        invoice = get_object_or_404(self.get_queryset(request), pk=object_id)
        try:
            restored = AdvanceDeductionService.remove(invoice)
        except ValidationError as e:
            self.message_user(request, " ".join(e.messages), level=messages.ERROR)
        else:
            if restored:
                self.message_user(request, _("Advance deductions removed (%(amount)s).") % {
                    "amount": number_format(restored, 2),
                })
            else:
                self.message_user(request, _("This invoice deducts no advances."), level=messages.WARNING)
        return self._dialog_redirect(object_id)

    @action(
        description=_("Reapply advances"), icon="replay",
        permissions=["change_salesinvoice"],
        dialog={
            "title": _("Reapply advances"),
            "description": _(
                "Removes this pro forma's advance deductions and deducts "
                "the default plan again."
            ),
            "form_submit_text": _("Reapply advances"),
        },
    )
    def reapply_advances_detail_action(self, request, form, object_id):
        """Deducts the default plan again, e.g. after its goods lines changed."""
        invoice = get_object_or_404(self.get_queryset(request), pk=object_id)
        try:
            with transaction.atomic():
                AdvanceDeductionService.remove(invoice)
                lines = AdvanceDeductionService.apply(invoice)
        except ValidationError as e:
            self.message_user(request, " ".join(e.messages), level=messages.ERROR)
        else:
            if lines:
                self.message_user(
                    request,
                    _("Advances deducted: %(amount)s (%(advances)s).")
                    % AdvanceDeductionService.describe(AdvanceDeductionService.deduction_lines(invoice)),
                )
            else:
                self.message_user(request, _("There are no advances to deduct."), level=messages.WARNING)
        return self._dialog_redirect(object_id)

    @display(
        description=_("Status"),
        ordering="status",
        label={
            SalesInvoiceStatus.FINAL: "success",
            SalesInvoiceStatus.PROFORMA: "warning",
            SalesInvoiceStatus.CANCELLED: "danger",
        },
    )
    def get_status_label(self, obj):
        return obj.status, obj.get_status_display()

    @display(
        description=_("Type"),
        ordering="document_type",
        label={
            SalesInvoiceDocumentType.INVOICE: "default",
            SalesInvoiceDocumentType.ADVANCE: "info",
        },
    )
    def get_document_type_label(self, obj):
        return obj.document_type, obj.get_document_type_display()

    @display(
        description=_("Number"),
        ordering=Coalesce("invoice_number", "proforma_number"),
    )
    def get_document_number(self, obj):
        return obj.display_number

    @display(
        description=_("Payment"),
        ordering="payment_status",
        label={
            InvoicePaymentStatus.UNPAID: "danger",
            InvoicePaymentStatus.PARTIAL: "warning",
            InvoicePaymentStatus.PAID: "success",
        },
    )
    def get_payment_status_label(self, obj):
        return obj.payment_status, obj.get_payment_status_display()

    @display(description=_("Late Commitment"))
    def get_late_commitment_warning(self, obj):
        if not obj.is_commitment_late:
            return "—"
        return format_html(
            '<span style="color:#d97706;font-weight:700;" title="{}">&#9888; {}</span>',
            _("Payment commitment date agreed more than 30 days after the due date"),
            _("LATE"),
        )

    @admin.action(description=_("Finalize selected pro forma invoices"))
    def finalize_invoice_action(self, request, queryset):
        count = 0
        failed = []
        # One invoice that cannot be finalized must not stop the others.
        for invoice in queryset.filter(status=SalesInvoiceStatus.PROFORMA):
            try:
                SalesInvoiceService.finalize_invoice(
                    invoice,
                    reply_to=sender_reply_to(request.user),
                    cc_emails=sender_cc(request.user),
                )
            except ValidationError as e:
                failed.append(f"{invoice.display_number}: {' '.join(e.messages)}")
                continue
            count += 1
        self.message_user(request, _("Finalized %d invoices.") % count)
        if failed:
            self.message_user(
                request,
                _("Could not finalize %(count)d invoices: %(invoices)s")
                % {"count": len(failed), "invoices": "; ".join(failed)},
                level=messages.ERROR,
            )

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
            invoice = queryset.first()
            return enqueue_document(
                request,
                render_sales_invoice_pdf,
                invoice_id=invoice.pk,
                filename=InvoicePdfDownloadService.sales_invoice_pdf_filename(invoice),
                back_url=reverse("admin:invoices_salesinvoice_changelist"),
            )

        result = build_invoice_pdf_zip_task.delay(
            "sales", invoice_ids, user_id=request.user.id
        )
        return redirect_to_document_status(
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
                "<int:object_id>/send-reminder-now/",
                self.admin_site.admin_view(
                    SendPaymentReminderNowAdminView.as_view(model_admin=self)
                ),
                name="invoices_salesinvoice_send_reminder_now",
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
            path(
                "<int:object_id>/register-payment/distribute-surplus/",
                self.admin_site.admin_view(
                    SalesInvoiceDistributeSurplusView.as_view(model_admin=self)
                ),
                name="invoices_salesinvoice_distribute_surplus",
            ),
            path(
                "<int:object_id>/apply-credit/",
                self.admin_site.admin_view(
                    SalesInvoiceApplyCreditView.as_view(model_admin=self)
                ),
                name="invoices_salesinvoice_apply_credit",
            ),
            path(
                "dashboard/collections-forecast/",
                self.admin_site.admin_view(
                    CollectionsForecastView.as_view(model_admin=self)
                ),
                name="invoices_salesinvoice_collections_forecast",
            ),
            path(
                "dashboard/collections-forecast/set-commitment-date/",
                self.admin_site.admin_view(
                    CollectionsForecastSetCommitmentDateView.as_view(model_admin=self)
                ),
                name="invoices_salesinvoice_collections_forecast_set_commitment_date",
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

    # Outcomes of journalizing one invoice, and how they're shown
    _JOURNALIZE_LEVELS = {
        "posted": messages.SUCCESS,
        "draft": messages.WARNING,
        "already": messages.WARNING,
        "not_issued": messages.ERROR,
        "error": messages.ERROR,
    }

    @staticmethod
    def _journalize_one(invoice):
        """
        Build (or rebuild a draft of) an issued invoice's journal entry and
        post it. Returns (outcome, message): outcome is a _JOURNALIZE_LEVELS key.
        """
        from accounting.models import EntryStatus
        from accounting.services.journal_generator import SalesJournalGeneratorService

        if invoice.status != SalesInvoiceStatus.FINAL:
            return "not_issued", _("Only issued invoices are journalized.")
        try:
            with transaction.atomic():
                entry = SalesJournalGeneratorService(invoice.company).retry_invoice(invoice)
        except SalesJournalGeneratorService.JournalizeError as e:
            return "error", str(e)
        except Exception:
            logger.exception("Could not journalize sales invoice %s", invoice.pk)
            return "error", _("The journal entry could not be created; the error was logged.")
        if entry is None:
            return "already", _("This invoice already has a posted journal entry.")
        if entry.status == EntryStatus.POSTED:
            return "posted", _("Journal entry %s posted.") % entry.number
        return "draft", _("Journal entry saved as draft: %s") % entry.post_error

    @action(description=_("Generate Journal Entry"), icon="post_add", permissions=["change_salesinvoice"])
    def journalize_detail_action(self, request, object_id):
        invoice = get_object_or_404(self.get_queryset(request), pk=object_id)
        outcome, message = self._journalize_one(invoice)
        self.message_user(request, message, level=self._JOURNALIZE_LEVELS[outcome])
        return redirect(reverse("admin:invoices_salesinvoice_change", args=[object_id]))

    @admin.action(description=_("Generate journal entries"), permissions=["change_salesinvoice"])
    def journalize_selected(self, request, queryset):
        results = {outcome: [] for outcome in self._JOURNALIZE_LEVELS}
        # By issue date, so the entries' numbers follow the invoices' dates
        for invoice in queryset.select_related("company").order_by("date_issued", "pk"):
            outcome, message = self._journalize_one(invoice)
            results[outcome].append(f"{invoice.display_number}: {message}")

        if results["posted"]:
            self.message_user(request, _("%(count)s journal entries posted.") % {"count": len(results["posted"])})
        if results["already"]:
            self.message_user(
                request, _("%(count)s already had a posted entry.") % {"count": len(results["already"])},
                level=messages.WARNING,
            )
        if results["not_issued"]:
            self.message_user(
                request, _("%(count)s are not issued and were skipped.") % {"count": len(results["not_issued"])},
                level=messages.WARNING,
            )
        problems = [(m, messages.ERROR) for m in results["error"]] + [(m, messages.WARNING) for m in results["draft"]]
        for message, level in problems[:10]:
            self.message_user(request, message, level=level)
        if len(problems) > 10:
            self.message_user(
                request, _("…and %(count)s more with problems.") % {"count": len(problems) - 10},
                level=messages.ERROR,
            )

    @action(description=_("Register Payment"), icon="payments")
    def register_payment_detail_action(self, request, object_id):
        return redirect(reverse("admin:invoices_salesinvoice_register_payment", args=[object_id]))

    def has_apply_credit_permission(self, request, object_id=None):
        if not object_id:
            return True
        invoice = SalesInvoice.objects.filter(pk=object_id).first()
        if not invoice:
            return False
        return SalesInvoiceApplyCreditView().can_apply_credit(request, invoice)

    @action(description=_("Apply Credit"), icon="account_balance_wallet", permissions=["apply_credit"])
    def apply_credit_detail_action(self, request, object_id):
        return redirect(reverse("admin:invoices_salesinvoice_apply_credit", args=[object_id]))

    @action(description=_("Send Email"), icon="mail")
    def send_email_row_action(self, request, object_id):
        invoice = SalesInvoice.objects.select_related("client").get(pk=object_id)
        if not invoice.client or not invoice.client.email:
            self.message_user(request, _("Client has no email address."), level="error")
        else:
            send_invoice_email_task.delay(
                invoice.id, reply_to=sender_reply_to(request.user)
            )
            self.message_user(request, _("Invoice email queued for %s.") % invoice.client.email)
        return redirect(reverse("admin:invoices_salesinvoice_changelist"))

    @action(description=_("Send Email"), icon="mail")
    def send_email_detail_action(self, request, object_id):
        invoice = SalesInvoice.objects.select_related("client").get(pk=object_id)
        if not invoice.client or not invoice.client.email:
            self.message_user(request, _("Client has no email address."), level="error")
        else:
            send_invoice_email_task.delay(
                invoice.id, reply_to=sender_reply_to(request.user)
            )
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

    def has_change_salesinvoice_permission(self, request, object_id=None):
        # A dotted "invoices.change_salesinvoice" in @action(permissions=...)
        # makes unfold's system check query auth_permission, which breaks
        # manage.py commands on a database that has not been migrated yet.
        return request.user.has_perm("invoices.change_salesinvoice")

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
            True: "info",
            False: "default",
        },
    )
    def get_external_label(self, obj):
        external = bool(obj.is_external)
        return external, _("External") if external else _("Internal")

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

    def _cancel_with_message(self, request, object_id):
        invoice = get_object_or_404(self.get_queryset(request), pk=object_id)
        if invoice.status == SalesInvoiceStatus.CANCELLED:
            self.message_user(request, _("Invoice is already cancelled."), level="warning")
            return
        # An advance still deducted or paid is refused here; the task checks
        # again, under lock, before anything is sent.
        try:
            SalesInvoiceService.validate_for_cancel(invoice)
        except ValidationError as e:
            self.message_user(request, " ".join(e.messages), level=messages.ERROR)
            return
        number = invoice.invoice_number or invoice.pk
        if not EInvoicingAccount.objects.filter(company_id=invoice.company_id, is_active=True).exists():
            # Cancelled locally, right away (checked again under lock, as the
            # task would: one already registered at the AEAT is refused)
            try:
                EInvoicingService.cancel_invoice(invoice)
            except ValidationError as e:
                self.message_user(request, " ".join(e.messages), level=messages.ERROR)
            else:
                self.message_user(
                    request,
                    _("Invoice %s cancelled locally: its company has no e-invoicing account.") % number,
                )
            return
        cancel_invoice_at_aeat.delay(invoice.id)
        self.message_user(request, _("AEAT cancellation queued for invoice %s.") % number)

    # Behind a dialog, like Finalize: it queues the cancellation or, without
    # an e-invoicing account, cancels the invoice right away
    _CANCEL_DIALOG = {
        "title": _("Cancel at AEAT"),
        "description": _(
            "Cancels this invoice at the AEAT. If its company has no "
            "e-invoicing account, it is cancelled here straight away. "
            "This cannot be undone."
        ),
        "form_submit_text": _("Cancel invoice"),
    }

    @action(
        description=_("Cancel at AEAT"), icon="cancel",
        permissions=["change_salesinvoice"], dialog=_CANCEL_DIALOG,
    )
    def cancel_aeat_row_action(self, request, form, object_id):
        self._cancel_with_message(request, object_id)
        return self._dialog_redirect()

    @action(
        description=_("Cancel at AEAT"), icon="cancel",
        permissions=["change_salesinvoice"], dialog=_CANCEL_DIALOG,
    )
    def cancel_aeat_detail_action(self, request, form, object_id):
        self._cancel_with_message(request, object_id)
        return self._dialog_redirect(object_id)

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


class PaymentReminderRuleInline(TabularInline):
    model = PaymentReminderRule
    extra = 1
    tab = False
    fields = ("days_before_due", "is_active")

@admin.register(PaymentReminderSettings)
class PaymentReminderSettingsAdmin(ModelAdmin, CompanyRestrictedAdmin):
    list_display = ("company", "send_time", "overdue_repeat_days", "language", "is_active", "last_run_date")
    list_filter = (
        ("company", AutocompleteSelectMultipleFilter),
        ("is_active", BooleanRadioFilter),
        ("language", ChoicesDropdownFilter),
    )
    autocomplete_fields = ("company",)
    readonly_fields = ("last_run_date",)
    inlines = [PaymentReminderRuleInline]
    formfield_overrides = {
        ArrayField: {
            "widget": ArrayWidget,
        }
    }

    fieldsets = (
        (
            _("General Information"),
            {
                "fields": (
                    "company",
                    "is_active",
                    "language",
                )
            },
        ),
        (
            _("Timing"),
            {
                "fields": (
                    "send_time",
                    "overdue_repeat_days",
                    "last_run_date",
                )
            },
        ),
        (
            _("Daily Report"),
            {
                "fields": ("report_emails",),
            },
        ),
    )

@admin.register(PaymentReminderLog)
class PaymentReminderLogAdmin(ModelAdmin, CompanyRestrictedAdmin):
    list_display = ("invoice", "company", "reminder_type", "scheduled_for", "status", "sent_at")
    list_filter = (
        ("company", AutocompleteSelectMultipleFilter),
        ("reminder_type", ChoicesDropdownFilter),
        ("status", ChoicesDropdownFilter),
        ("scheduled_for", RangeDateFilter),
    )
    search_fields = ("invoice__invoice_number", "invoice__client__name")
    autocomplete_fields = ("invoice",)
    list_select_related = ["invoice", "company", "rule"]
    readonly_fields = (
        "company", "invoice", "rule", "reminder_type",
        "scheduled_for", "status", "sent_at", "error_message",
    )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(CollectionsForecastEmailSettings)
class CollectionsForecastEmailSettingsAdmin(ModelAdmin, CompanyRestrictedAdmin):
    list_display = ("company", "is_active", "send_time", "frequency_days", "days_ahead", "late_commitment_threshold_days", "last_run_date")
    autocomplete_fields = ("company",)
    fields = ("company", "is_active", "send_time", "frequency_days", "days_ahead", "late_commitment_threshold_days", "report_emails", "last_run_date")
    readonly_fields = ("last_run_date",)
    formfield_overrides = {
        ArrayField: {
            "widget": ArrayWidget,
        }
    }


@admin.register(PaymentsForecastEmailSettings)
class PaymentsForecastEmailSettingsAdmin(ModelAdmin, CompanyRestrictedAdmin):
    list_display = ("company", "send_time", "frequency_days", "days_ahead", "is_active", "last_run_date")
    list_filter = (
        ("company", AutocompleteSelectMultipleFilter),
        ("is_active", BooleanRadioFilter),
    )
    autocomplete_fields = ("company",)
    readonly_fields = ("last_run_date",)
    formfield_overrides = {
        ArrayField: {
            "widget": ArrayWidget,
        }
    }

    def get_form(self, request, obj=None, change=False, **kwargs):
        # ArrayWidget doesn't auto-detect a field's choices -- report_emails
        # stays a free-text ArrayWidget (any email is valid), but
        # payment_methods needs to be constrained to PaymentMethod. Swapping
        # only the widget would leave SimpleArrayField.prepare_value() joining
        # the saved list into a comma string, which a multi-select can't
        # match back to its options -- so the field itself is replaced with a
        # plain MultipleChoiceField, which speaks lists natively.
        form = super().get_form(request, obj, change, **kwargs)
        original = form.base_fields["payment_methods"]
        form.base_fields["payment_methods"] = forms.MultipleChoiceField(
            choices=PaymentMethod.choices,
            required=False,
            label=original.label,
            help_text=original.help_text,
            widget=StaticSelect2MultipleWidget,
        )
        return form

    fieldsets = (
        (
            _("General Information"),
            {
                "fields": (
                    "company",
                    "is_active",
                )
            },
        ),
        (
            _("Timing"),
            {
                "fields": (
                    "send_time",
                    "frequency_days",
                    "days_ahead",
                    "last_run_date",
                )
            },
        ),
        (
            _("Filtering"),
            {
                "fields": ("payment_methods",),
                "description": _("Leave empty to include every payment method in the report."),
            },
        ),
        (
            _("Report"),
            {
                "fields": ("report_emails",),
            },
        ),
    )
