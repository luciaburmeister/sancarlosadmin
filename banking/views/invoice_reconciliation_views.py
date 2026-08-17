"""Views for the invoice-side reconciliation flow (reverse matcher)."""
import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import List

from django import forms
from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from django.views.generic import FormView

from unfold.views import UnfoldModelAdminViewMixin

from banking.models import (
    BankTransaction,
    BankReconciliation,
    BankTransactionCategory,
    InvoiceReconciliationSuggestion,
    ReconciliationStatus,
)
from banking.services.transaction_matching import InvoiceMatcher
from invoices.models import SalesInvoice
from apps.expenses.models import ExpenseClaim

logger = logging.getLogger(__name__)


@dataclass
class TransactionData:
    transaction_id: int
    available_amount: Decimal
    is_partial: bool
    date: object
    amount: Decimal
    description: str
    counterparty: str
    bank_account: str
    change_url: str


@dataclass
class CombinationData:
    index: int
    is_selected: bool
    confidence: str
    transactions: List[TransactionData]
    total: Decimal
    count: int


class CombinationSelectionForm(forms.Form):
    selected_combination = forms.IntegerField(min_value=0)


def _invoice_change_url(invoice):
    if isinstance(invoice, SalesInvoice):
        return reverse("admin:invoices_salesinvoice_change", args=[invoice.pk])
    if isinstance(invoice, ExpenseClaim):
        return reverse("admin:expenses_expenseclaim_change", args=[invoice.pk])
    return reverse("admin:invoices_invoice_change", args=[invoice.pk])


class ReviewInvoiceReconciliationView(UnfoldModelAdminViewMixin, FormView):
    """Display and approve an InvoiceReconciliationSuggestion."""

    title = _("Review Invoice Reconciliation")
    permission_required = ("banking.change_invoicereconciliationsuggestion",)
    template_name = "admin/banking/review_invoice_reconciliation.html"
    form_class = CombinationSelectionForm

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        suggestion = get_object_or_404(
            InvoiceReconciliationSuggestion, pk=self.kwargs["pk"]
        )
        invoice = suggestion.invoice

        invoice_change_url = _invoice_change_url(invoice)

        # Build enriched combinations — resolve transaction PKs to full objects
        combinations = []
        for idx, combo in enumerate(suggestion.suggested_combinations or []):
            txn_list = []
            total = Decimal("0")
            for txn_data in combo.get("transactions", []):
                try:
                    txn = BankTransaction.objects.select_related(
                        "bank_account"
                    ).get(pk=txn_data["transaction_id"])
                except BankTransaction.DoesNotExist:
                    continue

                available = Decimal(txn_data["available_amount"])
                total += available
                txn_list.append(TransactionData(
                    transaction_id=txn.pk,
                    available_amount=available,
                    is_partial=txn_data.get("is_partial", False),
                    date=txn.transaction_date,
                    amount=txn.amount,
                    description=txn.description or "—",
                    counterparty=txn.counterparty_name or "—",
                    bank_account=str(txn.bank_account),
                    change_url=reverse(
                        "admin:banking_banktransaction_change", args=[txn.pk]
                    ),
                ))

            combinations.append(CombinationData(
                index=idx,
                is_selected=combo.get("is_selected", False),
                confidence=combo.get("confidence", "none"),
                transactions=txn_list,
                total=total,
                count=len(txn_list),
            ))

        context.update({
            "suggestion": suggestion,
            "invoice": invoice,
            "invoice_number": getattr(invoice, "invoice_number", str(invoice)),
            "invoice_counterparty": str(
                getattr(invoice, "provider", None)
                or getattr(invoice, "client", None)
                or getattr(invoice, "submitted_by", None)
                or "—"
            ),
            "invoice_remaining": invoice.amount_remaining_decimal,
            "invoice_change_url": invoice_change_url,
            "combinations": combinations,
            "combo_count": len(combinations),
            "categories": BankTransactionCategory.objects.filter(
                company=suggestion.company
            ).order_by("name"),
            "back_url": invoice_change_url,
        })
        return context

    def form_valid(self, form):
        pk = self.kwargs["pk"]
        suggestion = get_object_or_404(InvoiceReconciliationSuggestion, pk=pk)
        invoice = suggestion.invoice
        invoice_url = _invoice_change_url(invoice)

        if suggestion.is_confirmed:
            messages.info(self.request, _("This reconciliation is already confirmed."))
            return redirect(invoice_url)

        selected_idx = form.cleaned_data["selected_combination"]
        combos = suggestion.suggested_combinations or []

        if selected_idx >= len(combos):
            messages.error(self.request, _("Invalid combination selected."))
            return redirect(self.request.path)

        # Persist the selection before confirming
        for idx, combo in enumerate(combos):
            combo["is_selected"] = idx == selected_idx
        suggestion.suggested_combinations = combos
        suggestion.save(update_fields=["suggested_combinations"])

        category = None
        category_id = self.request.POST.get("category")
        if category_id:
            category = BankTransactionCategory.objects.filter(
                pk=category_id, company_id=suggestion.company_id
            ).first()

        if category is None:
            messages.error(
                self.request,
                _("Please select a category before confirming this reconciliation."),
            )
            return redirect(self.request.path)

        try:
            InvoiceMatcher.confirm(suggestion, combos[selected_idx], self.request.user, category=category)
            messages.success(self.request, _("Reconciliation confirmed. Payment(s) created."))
        except Exception as e:
            logger.exception("Error confirming invoice reconciliation %s", pk)
            messages.error(
                self.request,
                _("Error confirming: %(error)s") % {"error": str(e)},
            )

        return redirect(invoice_url)
