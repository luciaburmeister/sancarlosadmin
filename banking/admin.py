"""Banking admin — BankTransaction and BankReconciliation."""
from unfold.contrib.filters.admin import RangeNumericFilter
from core.admin.company_admin import CompanyRestrictedAdmin
from core.admin.company_access import get_user_companies
from unfold.contrib.filters.admin import RangeDateFilter
from unfold.contrib.filters.admin import MultipleChoicesDropdownFilter
from unfold.contrib.filters.admin import AutocompleteSelectMultipleFilter
import logging

from django.contrib import admin
from django.shortcuts import get_object_or_404, redirect
from django.contrib import messages
from django.urls import path, reverse
from django.utils.html import format_html
from django.utils.translation import gettext_lazy as _
from import_export.admin import ImportExportModelAdmin
from unfold.admin import ModelAdmin, StackedInline
from unfold.contrib.import_export.forms import ImportForm, SelectableFieldsExportForm
from unfold.contrib.inlines.admin import NonrelatedTabularInline
from unfold.decorators import action, display

from banking.models import (
    BankReconciliation,
    BankTransaction,
    BankTransactionCategory,
    InvoiceReconciliationSuggestion,
    ReconciliationStatus,
)
from banking.resources import BankTransactionCategoryResource
from banking.views import (
    ConnectBankView,
    ConsentDoneView,
    InitiateConsentView,
    LinkTransactionView,
    ManualReconcileView,
    MatcherStatusPageView,
    MatcherTaskStatusAPIView,
    ReviewInvoiceReconciliationView,
    ReviewReconciliationView,
    SkipReconciliationView,
    SyncAccountsView,
)
from payments.models import PaymentAllocation

logger = logging.getLogger(__name__)


class BankReconciliationInline(StackedInline):
    """Read-only inline showing reconciliation status + link to review page."""
    model = BankReconciliation
    extra = 0
    max_num = 0
    can_delete = False
    tab = True
    fields = (
        "is_confirmed",
        "method",
        "payment",
        "unmatched_amount",
        "reconciled_by",
        "confirmed_at",
        "review_link",
    )
    readonly_fields = (
        "is_confirmed",
        "method",
        "payment",
        "unmatched_amount",
        "reconciled_by",
        "confirmed_at",
        "review_link",
    )

    @admin.display(description=_("Review"))
    def review_link(self, obj):
        if not obj.pk:
            return "—"
        url = reverse("admin:banking_reconciliation_review", args=[obj.pk])
        combo_count = len(obj.suggested_allocations or [])
        if obj.is_confirmed:
            return format_html(
                '<a href="{}" style="color: #16a34a; font-weight: 500;">'
                '✓ {}</a>',
                url, _("View details"),
            )
        label = _("Review & Approve")
        if combo_count > 1:
            label = _("Review & Approve (%(alt)d alternatives)") % {
                "alt": combo_count - 1
            }
        return format_html(
            '<a href="{}" style="color: var(--primary-600, #0369a1); '
            'font-weight: 600;">⚡ {}</a>',
            url, label,
        )


class PaymentAllocationInline(NonrelatedTabularInline):
    """Read-only inline showing payment allocations."""
    model = PaymentAllocation
    extra = 0
    max_num = 0
    can_delete = True
    tab = True
    fields = (
        "document_link",
        "document_date",
        "document_counterparty",
        "amount_applied",
    )
    readonly_fields = (
        "document_link",
        "document_date",
        "document_counterparty",
        "amount_applied",
    )

    def get_form_queryset(self, obj):
        """Return PaymentAllocations for this transaction's reconciliation payment."""
        try:
            recon = obj.reconciliation
            if recon.payment_id:
                return PaymentAllocation.objects.filter(
                    payment=recon.payment,
                ).select_related("content_type")
        except BankReconciliation.DoesNotExist:
            pass
        return PaymentAllocation.objects.none()

    def save_new_instance(self, parent, instance):
        """Required by NonrelatedTabularInline but we never create."""
        pass

    @admin.display(description=_("Document"))
    def document_link(self, obj):
        if not obj.pk:
            return "—"
        ct = obj.content_type
        doc = obj.related_object
        label = getattr(doc, "invoice_number", None) or str(doc)
        try:
            url = reverse(
                f"admin:{ct.app_label}_{ct.model}_change",
                args=[obj.object_id],
            )
            return format_html(
                '<a href="{}" target="_blank" style="color: var(--primary-600, #0369a1); '
                'font-weight: 500;">{}</a>',
                url, label,
            )
        except Exception:
            return label

    @admin.display(description=_("Date"))
    def document_date(self, obj):
        if not obj.pk:
            return "—"
        doc = obj.related_object
        date_issued = getattr(doc, "date_issued", None)
        if date_issued is not None:
            return date_issued
        created_at = getattr(doc, "created_at", None)
        if created_at is not None:
            return created_at.date() if hasattr(created_at, "date") else created_at
        return "—"

    @admin.display(description=_("Counterparty"))
    def document_counterparty(self, obj):
        if not obj.pk:
            return "—"
        doc = obj.related_object
        return str(
            getattr(doc, "client", None)
            or getattr(doc, "provider", None)
            or getattr(doc, "submitted_by", None)
            or "—"
        )


@admin.register(BankTransactionCategory)
class BankTransactionCategoryAdmin(CompanyRestrictedAdmin, ModelAdmin, ImportExportModelAdmin):
    resource_classes = [BankTransactionCategoryResource]
    import_form_class = ImportForm
    export_form_class = SelectableFieldsExportForm

    list_display = ("name", "company")
    list_filter = (("company", AutocompleteSelectMultipleFilter),)
    list_filter_submit = True
    search_fields = ("name",)
    autocomplete_fields = ("company",)
    ordering = ("company__name", "name")


@admin.register(BankTransaction)
class BankTransactionAdmin(CompanyRestrictedAdmin, ModelAdmin):
    list_display = (
        "id",
        "transaction_date",
        "display_amount",
        "counterparty_name",
        "description_short",
        "get_reconciliation_label",
        "category",
        "bank_account",
    )
    list_filter = (
        ("company",AutocompleteSelectMultipleFilter),
        ("reconciliation_status",MultipleChoicesDropdownFilter),
        ("amount",RangeNumericFilter),
        ("bank_account",AutocompleteSelectMultipleFilter),
        ("category",AutocompleteSelectMultipleFilter),
        ("transaction_date",RangeDateFilter)
    )
    list_filter_submit = True
    search_fields = (
        "description",
        "counterparty_name",
        "counterparty_iban",
        "external_id",
    )
    readonly_fields = (
        "external_id",
        "raw_data",
        "imported_at",
    )
    list_select_related = ("bank_account", "category")
    ordering = ("-transaction_date", "-imported_at")
    list_per_page = 30

    actions_detail = ["run_matcher_detail_action", "review_suggestion_action", "manual_reconcile_action"]
    inlines = [BankReconciliationInline, PaymentAllocationInline]

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        """
        The category dropdown (plain select, no autocomplete) only lists
        categories belonging to this transaction's own company, sorted by name.
        """
        if db_field.name == "category":
            object_id = request.resolver_match.kwargs.get("object_id") if request.resolver_match else None
            if object_id:
                txn = BankTransaction.objects.filter(pk=object_id).only("company_id").first()
                if txn and txn.company_id:
                    kwargs["queryset"] = BankTransactionCategory.objects.filter(
                        company_id=txn.company_id
                    ).order_by("name")
                else:
                    kwargs["queryset"] = BankTransactionCategory.objects.none()
            else:
                qs = BankTransactionCategory.objects.all()
                if not request.user.is_superuser:
                    qs = qs.filter(company__in=get_user_companies(request.user))
                kwargs["queryset"] = qs.order_by("company__name", "name")

        return super().formfield_for_foreignkey(db_field, request, **kwargs)

    def get_urls(self):
        custom_urls = [
            path(
                "connect/",
                self.admin_site.admin_view(
                    ConnectBankView.as_view(model_admin=self)
                ),
                name="banking_connect_bank",
            ),
            path(
                "connect/initiate/",
                self.admin_site.admin_view(
                    InitiateConsentView.as_view(model_admin=self)
                ),
                name="banking_initiate_consent",
            ),
            path(
                "connect/done/",
                self.admin_site.admin_view(
                    ConsentDoneView.as_view(model_admin=self)
                ),
                name="banking_consent_done",
            ),
            path(
                "sync/",
                self.admin_site.admin_view(
                    SyncAccountsView.as_view(model_admin=self)
                ),
                name="banking_sync",
            ),
            path(
                "<int:pk>/manual-reconcile/",
                self.admin_site.admin_view(
                    ManualReconcileView.as_view(model_admin=self)
                ),
                name="banking_banktransaction_manual_reconcile",
            ),
            path(
                "payment/<int:payment_id>/link-transaction/",
                self.admin_site.admin_view(
                    LinkTransactionView.as_view(model_admin=self)
                ),
                name="banking_payment_link_transaction",
            ),
            path(
                "matcher-status/",
                self.admin_site.admin_view(
                    MatcherStatusPageView.as_view(model_admin=self)
                ),
                name="banking_matcher_status",
            ),
            path(
                "matcher-status/<str:task_id>/api/",
                self.admin_site.admin_view(
                    MatcherTaskStatusAPIView.as_view(model_admin=self)
                ),
                name="banking_matcher_status_api",
            ),
        ]
        return custom_urls + super().get_urls()

    @admin.display(description=_("Amount"))
    def display_amount(self, obj):
        return obj.amount

    @admin.display(description=_("Description"))
    def description_short(self, obj):
        return (obj.description or "")[:80]

    @display(
        description=_("Reconciliation"),
        ordering="reconciliation_status",
        label={
            ReconciliationStatus.UNRECONCILED: "warning",
            ReconciliationStatus.PARTIAL: "info",
            ReconciliationStatus.RECONCILED: "success",
        },
    )
    def get_reconciliation_label(self, obj):
        return obj.reconciliation_status

    @action(
        description=_("Review Suggestion"),
        url_path="review-suggestion",
        icon="rate_review",
    )
    def review_suggestion_action(self, request, object_id: int):
        """Redirect to the review page for this transaction's reconciliation."""
        txn = get_object_or_404(BankTransaction, pk=object_id)
        try:
            recon = txn.reconciliation
            return redirect(
                reverse("admin:banking_reconciliation_review", args=[recon.pk])
            )
        except BankReconciliation.DoesNotExist:
            messages.info(
                request,
                _("No reconciliation suggestion exists. Run the matcher first."),
            )
            return redirect(request.META.get("HTTP_REFERER"))

    @action(
        description=_("Run Matcher"),
        url_path="run-matcher",
        icon="compare_arrows",
    )
    def run_matcher_detail_action(self, request, object_id: int):
        from banking.tasks import run_matcher_for_transaction

        txn = get_object_or_404(BankTransaction, pk=object_id)

        # Don't re-run if already reconciled
        if txn.reconciliation_status == ReconciliationStatus.RECONCILED:
            messages.warning(
                request,
                _("This transaction is already reconciled."),
            )
            return redirect(request.META.get("HTTP_REFERER"))

        # Dispatch to Celery and redirect to the status page
        result = run_matcher_for_transaction.delay(txn.pk)
        back_url = reverse("admin:banking_banktransaction_change", args=[txn.pk])
        status_url = (
            reverse("admin:banking_matcher_status")
            + f"?task_id={result.id}&task_type=transaction&back_url={back_url}"
        )
        return redirect(status_url)

    @action(
        description=_("Reconcile Manually"),
        url_path="manual-reconcile",
        icon="edit_note",
    )
    def manual_reconcile_action(self, request, object_id: int):
        txn = get_object_or_404(BankTransaction, pk=object_id)
        if txn.reconciliation_status == ReconciliationStatus.RECONCILED:
            messages.warning(request, _("This transaction is already reconciled."))
            return redirect(request.META.get("HTTP_REFERER"))
        return redirect(
            reverse("admin:banking_banktransaction_manual_reconcile", args=[object_id])
        )


@admin.register(BankReconciliation)
class BankReconciliationAdmin(ModelAdmin):
    list_display = (
        "id",
        "bank_transaction",
        "payment",
        "get_confirmed_label",
        "combo_count_display",
        "method",
        "reconciled_at",
        "confirmed_at",
        "review_action",
    )
    list_filter = (
        "is_confirmed",
        "method",
        ("company",AutocompleteSelectMultipleFilter)
    )
    search_fields = (
        "bank_transaction__description",
        "bank_transaction__counterparty_name",
    )
    readonly_fields = (
        "is_confirmed",
        "suggested_allocations",
        "reconciled_at",
    )
    list_select_related = ("bank_transaction", "payment")
    autocomplete_fields = ("bank_transaction", "payment")
    ordering = ("-reconciled_at",)
    list_per_page = 30

    def get_urls(self):
        custom_urls = [
            path(
                "<int:pk>/review/",
                self.admin_site.admin_view(
                    ReviewReconciliationView.as_view(model_admin=self)
                ),
                name="banking_reconciliation_review",
            ),
            path(
                "<int:pk>/skip/",
                self.admin_site.admin_view(
                    SkipReconciliationView.as_view(model_admin=self)
                ),
                name="banking_reconciliation_skip",
            ),
        ]
        return custom_urls + super().get_urls()

    @display(
        description=_("Confirmed"),
        boolean=True,
    )
    def get_confirmed_label(self, obj):
        return obj.is_confirmed

    @admin.display(description=_("Combos"))
    def combo_count_display(self, obj):
        count = len(obj.suggested_allocations or [])
        if count == 0:
            return "—"
        return count

    @admin.display(description=_("Action"))
    def review_action(self, obj):
        url = reverse("admin:banking_reconciliation_review", args=[obj.pk])
        if obj.is_confirmed:
            return format_html(
                '<a href="{}" style="color: #16a34a;">✓ {}</a>',
                url, _("View"),
            )
        return format_html(
            '<a href="{}" style="color: var(--primary-600, #0369a1); '
            'font-weight: 600;">⚡ {}</a>',
            url, _("Review"),
        )


@admin.register(InvoiceReconciliationSuggestion)
class InvoiceReconciliationSuggestionAdmin(ModelAdmin):
    list_display = ("id", "content_type", "object_id", "get_confirmed_label", "created_at")
    list_filter = ("is_confirmed", ("company", AutocompleteSelectMultipleFilter))
    readonly_fields = ("content_type", "object_id", "suggested_combinations", "is_confirmed", "created_at")
    ordering = ("-created_at",)
    list_per_page = 30
    actions_row = ["review_action"]

    def get_urls(self):
        custom_urls = [
            path(
                "<int:pk>/review/",
                self.admin_site.admin_view(
                    ReviewInvoiceReconciliationView.as_view(model_admin=self)
                ),
                name="banking_invoicereconciliationsuggestion_review",
            ),
        ]
        return custom_urls + super().get_urls()

    @display(description=_("Confirmed"), boolean=True)
    def get_confirmed_label(self, obj):
        return obj.is_confirmed

    @action(description=_("Review"), url_path="review", icon="rate_review")
    def review_action(self, request, object_id: int):
        suggestion = get_object_or_404(InvoiceReconciliationSuggestion, pk=object_id)
        if suggestion.is_confirmed:
            messages.info(request, _("This reconciliation is already confirmed."))
            return redirect(request.META.get("HTTP_REFERER"))
        return redirect(
            reverse("admin:banking_invoicereconciliationsuggestion_review", args=[object_id])
        )
