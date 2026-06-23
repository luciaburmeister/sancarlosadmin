"""Banking admin — BankTransaction and BankReconciliation."""
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
from unfold.admin import ModelAdmin, StackedInline
from unfold.contrib.inlines.admin import NonrelatedTabularInline
from unfold.decorators import action, display

from banking.models import BankReconciliation, BankTransaction, ReconciliationStatus
from banking.views import (
    ApproveReconciliationView,
    ConnectBankView,
    ConsentDoneView,
    InitiateConsentView,
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
        "confirmed_at",
        "review_link",
    )
    readonly_fields = (
        "is_confirmed",
        "method",
        "payment",
        "unmatched_amount",
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
    """Non-related inline showing payment allocations via the reconciliation chain."""
    model = PaymentAllocation
    extra = 0
    can_delete = False
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
        label = getattr(doc, "invoice_number", str(doc))
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
        return getattr(doc, "date_issued", "—")

    @admin.display(description=_("Counterparty"))
    def document_counterparty(self, obj):
        if not obj.pk:
            return "—"
        doc = obj.related_object
        return str(
            getattr(doc, "client", None)
            or getattr(doc, "provider", None)
            or "—"
        )


@admin.register(BankTransaction)
class BankTransactionAdmin(ModelAdmin):
    list_display = (
        "id",
        "transaction_date",
        "display_amount",
        "counterparty_name",
        "description_short",
        "get_reconciliation_label",
        "bank_account",
    )
    list_filter = (
        ("reconciliation_status",MultipleChoicesDropdownFilter),
        ("bank_account",AutocompleteSelectMultipleFilter),
        ("transaction_date",RangeDateFilter),
        ("company",AutocompleteSelectMultipleFilter)
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
    list_select_related = ("bank_account",)
    ordering = ("-transaction_date", "-imported_at")
    list_per_page = 30

    actions_detail = ["run_matcher_detail_action", "review_suggestion_action"]
    inlines = [BankReconciliationInline, PaymentAllocationInline]

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
        from banking.services.transaction_matching import TransactionMatcher

        txn = get_object_or_404(BankTransaction, pk=object_id)

        # Don't re-run if already reconciled
        if txn.reconciliation_status == ReconciliationStatus.RECONCILED:
            messages.warning(
                request,
                _("This transaction is already reconciled."),
            )
            return redirect(request.META.get("HTTP_REFERER"))

        # Delete any existing unconfirmed suggestion so we can re-run
        BankReconciliation.objects.filter(
            bank_transaction=txn, is_confirmed=False,
        ).delete()

        try:
            matcher = TransactionMatcher(txn)
            recon = matcher.run()
            selected = TransactionMatcher.get_selected_allocations(
                recon.suggested_allocations,
            )
            if selected:
                # Redirect directly to review page
                return redirect(
                    reverse("admin:banking_reconciliation_review", args=[recon.pk])
                )
            else:
                messages.info(
                    request,
                    _("Matcher ran but found no matching documents. A reconciliation record was created for manual review."),
                )
        except Exception as e:
            logger.exception("Error running matcher for transaction %s", object_id)
            messages.error(
                request,
                _("Error running matcher: %(error)s") % {"error": str(e)},
            )

        return redirect(request.META.get("HTTP_REFERER"))


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
        "suggested_allocations",
        "reconciled_at",
    )
    list_select_related = ("bank_transaction", "payment")
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
                "<int:pk>/approve/",
                self.admin_site.admin_view(
                    ApproveReconciliationView.as_view(model_admin=self)
                ),
                name="banking_reconciliation_approve",
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
