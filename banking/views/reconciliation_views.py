import logging
from datetime import timedelta
from decimal import Decimal

from django import forms
from django.apps import apps
from django.contrib import messages
from django.contrib.contenttypes.models import ContentType
from django.db import transaction as db_transaction
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from django.views import View
from django.views.generic import FormView, TemplateView
from djmoney.money import Money

from unfold.views import UnfoldModelAdminViewMixin

from banking.models import (
    BankReconciliation,
    BankTransaction,
    BankTransactionCategory,
    ReconciliationMethod,
    ReconciliationStatus,
)
from banking.services.transaction_matching import TransactionMatcher, candidates
from banking.services.transaction_matching.types import PAYMENT_DATE_TOLERANCE_DAYS
from payments.models import Payment, PaymentAllocation, PaymentDirection

logger = logging.getLogger(__name__)


class CombinationSelectionForm(forms.Form):
    selected_combination = forms.IntegerField(min_value=0)


class ReviewReconciliationView(UnfoldModelAdminViewMixin, FormView):
    """Display and approve a reconciliation suggestion."""

    title = _("Review Reconciliation")
    permission_required = ("banking.change_bankreconciliation",)
    template_name = "admin/banking/review_reconciliation.html"
    form_class = CombinationSelectionForm

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        recon = get_object_or_404(
            BankReconciliation.objects.select_related(
                "bank_transaction__company",
                "bank_transaction__bank_account",
            ),
            pk=self.kwargs["pk"],
        )
        txn = recon.bank_transaction

        # Build enriched combinations with resolved document details
        combinations = []
        for idx, combo in enumerate(recon.suggested_allocations or []):
            allocations = combo.get("allocations", [])
            docs = []
            total = Decimal("0")
            for alloc in allocations:
                ct = ContentType.objects.get(model=alloc["content_type"])
                doc = ct.get_object_for_this_type(pk=alloc["object_id"])
                remaining = doc.amount_remaining_decimal
                total += remaining

                # Build admin change URL for the document
                app_label = ct.app_label
                model_name = ct.model
                try:
                    change_url = reverse(
                        f"admin:{app_label}_{model_name}_change",
                        args=[doc.pk],
                    )
                except Exception:
                    change_url = None

                # Date/counterparty live under different names per document
                # type: invoices use date_issued/client/provider, loan
                # installments use due_date/loan.bank, loan events use
                # date/parent_loan.bank, expense claims use
                # created_at/submitted_by.
                doc_date = (
                    getattr(doc, "date_issued", None)
                    or getattr(doc, "due_date", None)
                    or getattr(doc, "date", None)
                )
                if doc_date is None and getattr(doc, "created_at", None):
                    doc_date = doc.created_at.date()

                loan = getattr(doc, "loan", None) or getattr(doc, "parent_loan", None)
                counterparty = (
                    getattr(doc, "client", None)
                    or getattr(doc, "provider", None)
                    or (loan.bank if loan else None)
                    or getattr(doc, "submitted_by", None)
                )

                docs.append({
                    "content_type": alloc["content_type"],
                    "object_id": doc.pk,
                    "number": getattr(doc, "invoice_number", str(doc)),
                    "date": doc_date,
                    "counterparty": str(counterparty or "—"),
                    "remaining": remaining,
                    "change_url": change_url,
                })

            # The movement is a payment registered before it reached the bank
            payment = None
            if combo.get("payment_id"):
                payment = Payment.objects.filter(pk=combo["payment_id"], company_id=txn.company_id).first()
                if payment is None:
                    continue
                total = payment.amount.amount

            combinations.append({
                "index": idx,
                "is_selected": combo.get("is_selected", False),
                "confidence": combo.get("confidence", "none"),
                "docs": docs,
                "payment": payment,
                "payment_url": reverse("admin:payments_payment_change", args=[payment.pk]) if payment else None,
                "total": total,
                "count": len(docs),
            })

        txn_amount = abs(txn.amount.amount)

        context.update({
            "title": self.title,
            "recon": recon,
            "txn": txn,
            "txn_amount": txn_amount,
            "combinations": combinations,
            "combo_count": len(combinations),
            "categories": BankTransactionCategory.objects.filter(
                company=txn.company
            ).order_by("name"),
            "skip_url": reverse(
                "admin:banking_reconciliation_skip",
                args=[recon.pk],
            ),
            "back_url": reverse("admin:banking_banktransaction_changelist"),
        })
        return context

    def form_valid(self, form):
        pk = self.kwargs["pk"]
        recon = get_object_or_404(
            BankReconciliation.objects.select_related("bank_transaction__company"),
            pk=pk,
        )

        if recon.is_confirmed:
            messages.info(self.request, _("This reconciliation is already confirmed."))
            return redirect(reverse("admin:banking_banktransaction_changelist"))

        selected_idx = form.cleaned_data["selected_combination"]
        combos = recon.suggested_allocations or []

        if selected_idx >= len(combos):
            messages.error(self.request, _("Invalid combination selected."))
            return redirect(self.request.path)

        # Update is_selected flags
        for idx, combo in enumerate(combos):
            combo["is_selected"] = (idx == selected_idx)
        recon.suggested_allocations = combos
        recon.save(update_fields=["suggested_allocations"])

        confirmed_allocations = combos[selected_idx].get("allocations", [])

        category = None
        category_id = self.request.POST.get("category")
        if category_id:
            category = BankTransactionCategory.objects.filter(
                pk=category_id, company_id=recon.bank_transaction.company_id
            ).first()

        payment_id = combos[selected_idx].get("payment_id")
        if payment_id:
            payment = Payment.objects.filter(pk=payment_id, company_id=recon.bank_transaction.company_id).first()
            try:
                if payment is None:
                    raise ValueError(_("The payment of this suggestion no longer exists."))
                TransactionMatcher.confirm_existing_payment(
                    recon.bank_transaction, payment, self.request.user,
                    method=ReconciliationMethod.MANUAL, category=category,
                )
                messages.success(self.request, _("Reconciliation confirmed. Linked to the existing payment."))
            except ValueError as e:
                messages.error(self.request, str(e))
                return redirect(self.request.path)
            return redirect(reverse("admin:banking_banktransaction_changelist"))

        if category is None and not confirmed_allocations:
            messages.error(
                self.request,
                _("Please select a category before confirming this reconciliation."),
            )
            return redirect(self.request.path)

        try:
            TransactionMatcher.confirm(
                reconciliation=recon,
                confirmed_allocations=confirmed_allocations,
                user=self.request.user,
                category=category,
            )
            messages.success(self.request, _("Reconciliation confirmed. Payment created."))
        except Exception as e:
            logger.exception("Error confirming reconciliation %s", pk)
            messages.error(
                self.request,
                _("Error confirming: %(error)s") % {"error": str(e)},
            )

        return redirect(reverse("admin:banking_banktransaction_changelist"))


class SkipReconciliationView(UnfoldModelAdminViewMixin, View):
    """POST handler — reject/skip a suggestion (deletes unconfirmed recon)."""

    title = _("Skip Reconciliation")
    permission_required = ("banking.change_bankreconciliation",)

    def post(self, request, pk):
        recon = get_object_or_404(BankReconciliation, pk=pk)

        if recon.is_confirmed:
            messages.warning(request, _("Cannot skip an already confirmed reconciliation."))
        else:
            recon.delete()
            messages.info(request, _("Reconciliation suggestion skipped."))

        return redirect(reverse("admin:banking_banktransaction_changelist"))


class CategoryReconcileView(UnfoldModelAdminViewMixin, View):
    """POST handler — reconcile a movement with no document behind it to a category's account."""

    title = _("Book to Category")
    permission_required = ("banking.change_banktransaction",)

    def post(self, request, pk):
        txn = get_object_or_404(BankTransaction, pk=pk)
        category = BankTransactionCategory.objects.filter(
            pk=request.POST.get("category") or None, company_id=txn.company_id,
        ).first()
        if category is None:
            messages.error(request, _("Please select a category before confirming this reconciliation."))
            return redirect(reverse("admin:banking_banktransaction_manual_reconcile", args=[pk]))
        try:
            TransactionMatcher.confirm_without_documents(txn, category, request.user)
        except ValueError as e:
            messages.error(request, str(e))
            return redirect(reverse("admin:banking_banktransaction_manual_reconcile", args=[pk]))
        messages.success(
            request,
            _("Reconciled and booked to %(account)s.") % {"account": category.account},
        )
        return redirect(reverse("admin:banking_banktransaction_changelist"))


class ManualReconcileView(UnfoldModelAdminViewMixin, TemplateView):
    """Manual reconciliation — user picks invoices directly without the matcher."""

    title = _("Reconcile Manually")
    permission_required = ("banking.change_banktransaction",)
    template_name = "admin/banking/manual_reconcile.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        txn = get_object_or_404(
            BankTransaction.objects.select_related("bank_account__account", "company"),
            pk=self.kwargs["pk"],
        )

        is_partial = txn.reconciliation_status == ReconciliationStatus.PARTIAL
        if is_partial:
            try:
                existing_recon = txn.reconciliation
                effective_amount = existing_recon.unmatched_amount.amount
            except BankReconciliation.DoesNotExist:
                is_partial = False
                effective_amount = abs(txn.amount.amount)
        else:
            effective_amount = abs(txn.amount.amount)

        documents = self._get_candidate_documents(txn)
        kinds = {
            ct.model: ct.model_class()._meta.verbose_name
            for ct in ContentType.objects.filter(model__in={doc["content_type"] for doc in documents})
            if ct.model_class()
        }
        for doc in documents:
            doc["kind"] = kinds.get(doc["content_type"], doc["content_type"])

        bank_ledger = txn.bank_account.account
        context.update({
            "txn": txn,
            "txn_amount": effective_amount,
            "is_partial": is_partial,
            "is_inbound": txn.amount.amount > 0,
            "documents": documents,
            # The bank's own ledger account is opened on its first booking
            "bank_ledger_label": f"{bank_ledger.code} {bank_ledger.name}" if bank_ledger else _("Bank (572…)"),
            "categories_url": reverse("admin:banking_banktransactioncategory_changelist"),
            "submit_url": reverse(
                "admin:banking_banktransaction_manual_reconcile",
                args=[txn.pk],
            ),
            # Movements no document backs are booked to a category's account
            "account_categories": BankTransactionCategory.objects.filter(
                company=txn.company, account__isnull=False,
            ).select_related("account").order_by("name"),
            "category_reconcile_url": reverse(
                "admin:banking_banktransaction_reconcile_category",
                args=[txn.pk],
            ),
            "back_url": reverse(
                "admin:banking_banktransaction_change",
                args=[txn.pk],
            ),
        })
        return context

    def post(self, request, pk):
        txn = get_object_or_404(BankTransaction, pk=pk)

        if txn.reconciliation_status == ReconciliationStatus.RECONCILED:
            messages.warning(request, _("This transaction is already reconciled."))
            return redirect(reverse("admin:banking_banktransaction_change", args=[pk]))

        selected_ids = request.POST.getlist("selected_docs")
        if not selected_ids:
            messages.error(request, _("Please select at least one document."))
            return redirect(reverse("admin:banking_banktransaction_manual_reconcile", args=[pk]))

        # Build allocation list from selected doc IDs (format: "content_type:object_id")
        confirmed_allocations = []
        for entry in selected_ids:
            try:
                ct_model, obj_id = entry.split(":")
                confirmed_allocations.append({
                    "content_type": ct_model,
                    "object_id": int(obj_id),
                })
            except (ValueError, TypeError):
                continue

        if not confirmed_allocations:
            messages.error(request, _("Invalid document selection."))
            return redirect(reverse("admin:banking_banktransaction_manual_reconcile", args=[pk]))

        category = None
        category_id = request.POST.get("category")
        if category_id:
            category = BankTransactionCategory.objects.filter(
                pk=category_id, company_id=txn.company_id
            ).first()

        is_partial = txn.reconciliation_status == ReconciliationStatus.PARTIAL
        existing_recon = None
        if is_partial:
            try:
                existing_recon = txn.reconciliation
            except BankReconciliation.DoesNotExist:
                is_partial = False

        try:
            if is_partial and existing_recon:
                self._add_allocations_to_existing(
                    reconciliation=existing_recon,
                    confirmed_allocations=confirmed_allocations,
                    category=category,
                )
            else:
                BankReconciliation.objects.filter(bank_transaction=txn, is_confirmed=False).delete()
                recon = BankReconciliation.objects.create(
                    bank_transaction=txn,
                    payment=None,
                    is_confirmed=False,
                    method=ReconciliationMethod.MANUAL,
                    suggested_allocations=[],
                    unmatched_amount=Money(0, txn.amount_currency),
                )
                TransactionMatcher.confirm(
                    reconciliation=recon,
                    confirmed_allocations=confirmed_allocations,
                    user=request.user,
                    category=category,
                )
            messages.success(request, _("Reconciliation confirmed successfully."))
        except Exception as e:
            logger.exception("Error during manual reconciliation for transaction %s", pk)
            messages.error(request, _("Error confirming reconciliation: %(error)s") % {"error": str(e)})

        return redirect(reverse("admin:banking_banktransaction_changelist"))

    @staticmethod
    @db_transaction.atomic
    def _add_allocations_to_existing(
        reconciliation: BankReconciliation,
        confirmed_allocations: list,
        category: BankTransactionCategory | None = None,
    ) -> None:
        """Add more allocations to an already-confirmed PARTIAL reconciliation."""
        payment = reconciliation.payment
        txn = reconciliation.bank_transaction

        current_unmatched = reconciliation.unmatched_amount.amount
        total_new = Decimal("0")
        for allocation in confirmed_allocations:
            budget = current_unmatched - total_new
            if budget <= 0:
                break
            ct = ContentType.objects.get(model=allocation["content_type"])
            doc = ct.get_object_for_this_type(pk=allocation["object_id"])
            remaining = doc.amount_remaining_decimal
            if remaining <= 0:
                continue
            amount_to_apply = min(remaining, budget)
            PaymentAllocation.objects.create(
                payment=payment,
                content_type=ct,
                object_id=allocation["object_id"],
                amount_applied=amount_to_apply,
            )
            total_new += amount_to_apply

        new_unmatched = max(Decimal("0"), current_unmatched - total_new)
        reconciliation.unmatched_amount = Money(new_unmatched, txn.amount_currency)
        reconciliation.save(update_fields=["unmatched_amount"])

        update_fields = []
        if new_unmatched <= Decimal("0.01"):
            txn.reconciliation_status = ReconciliationStatus.RECONCILED
            update_fields.append("reconciliation_status")
        if category is not None:
            txn.category = category
            update_fields.append("category")
        if update_fields:
            txn.save(update_fields=update_fields)

    @staticmethod
    def _get_candidate_documents(txn: BankTransaction) -> list:
        """Return unpaid invoices, sales invoices, loan installments, or
        approved expense claims for this transaction's company.

        Documents dated up to PAYMENT_DATE_TOLERANCE_DAYS after the
        transaction date are included too, so a payment made slightly
        before the document's date (early payments) is still selectable.
        """
        Invoice = apps.get_model("invoices", "Invoice")
        SalesInvoice = apps.get_model("invoices", "SalesInvoice")
        LoanInstallment = apps.get_model("loans", "LoanInstallment")
        ExpenseClaim = apps.get_model("expenses", "ExpenseClaim")

        cutoff_date = txn.transaction_date + timedelta(days=PAYMENT_DATE_TOLERANCE_DAYS)
        direction = PaymentDirection.INBOUND if txn.amount.amount > 0 else PaymentDirection.OUTBOUND
        results = []

        if direction == PaymentDirection.OUTBOUND:
            qs = candidates.invoice_candidates(txn.company, cutoff_date).select_related("provider").order_by("-date_issued")

            ct = ContentType.objects.get_for_model(Invoice)
            for doc in qs:
                results.append({
                    "content_type": ct.model,
                    "object_id": doc.pk,
                    "form_value": f"{ct.model}:{doc.pk}",
                    "number": getattr(doc, "invoice_number", str(doc)),
                    "date": getattr(doc, "date_issued", None),
                    "counterparty": str(getattr(doc, "provider", None) or "—"),
                    "remaining": doc.amount_remaining_decimal,
                })

            installment_qs = candidates.loan_installment_candidates(
                txn.company, txn.bank_account, cutoff_date
            ).select_related("loan", "loan__bank").order_by("due_date")

            ict = ContentType.objects.get_for_model(LoanInstallment)
            for installment in installment_qs:
                results.append({
                    "content_type": ict.model,
                    "object_id": installment.pk,
                    "form_value": f"{ict.model}:{installment.pk}",
                    "number": str(installment),
                    "date": installment.due_date,
                    "counterparty": str(installment.loan.bank),
                    "remaining": installment.amount_remaining_decimal,
                })

            results += self._tax_installment_rows(txn.company, cutoff_date, refund=False)

            claim_qs = candidates.expense_claim_candidates(
                txn.company, cutoff_date
            ).select_related("submitted_by").order_by("-created_at")

            cct = ContentType.objects.get_for_model(ExpenseClaim)
            for claim in claim_qs:
                if claim.amount_remaining_decimal <= 0:
                    continue
                results.append({
                    "content_type": cct.model,
                    "object_id": claim.pk,
                    "form_value": f"{cct.model}:{claim.pk}",
                    "number": str(claim),
                    "date": claim.created_at.date(),
                    "counterparty": claim.submitted_by.get_full_name() or str(claim.submitted_by),
                    "remaining": claim.amount_remaining_decimal,
                })
        else:
            qs = candidates.sales_invoice_candidates(txn.company, cutoff_date).select_related("client").order_by("-date_issued")

            ct = ContentType.objects.get_for_model(SalesInvoice)
            for doc in qs:
                results.append({
                    "content_type": ct.model,
                    "object_id": doc.pk,
                    "form_value": f"{ct.model}:{doc.pk}",
                    "number": getattr(doc, "invoice_number", str(doc)),
                    "date": getattr(doc, "date_issued", None),
                    "counterparty": str(getattr(doc, "client", None) or "—"),
                    "remaining": doc.amount_remaining_decimal,
                })

            results += self._tax_installment_rows(txn.company, cutoff_date, refund=True)

        return results

    @staticmethod
    def _tax_installment_rows(company, cutoff_date, refund):
        TaxInstallment = apps.get_model("finance", "TaxInstallment")
        tax_installment_qs = candidates.tax_installment_candidates(
            company, cutoff_date, refund=refund
        ).select_related("planned_tax_payment__tax_type").order_by("due_date")

        tict = ContentType.objects.get_for_model(TaxInstallment)
        return [
            {
                "content_type": tict.model,
                "object_id": installment.pk,
                "form_value": f"{tict.model}:{installment.pk}",
                "number": str(installment),
                "date": installment.due_date,
                "counterparty": str(installment.planned_tax_payment.tax_type),
                "remaining": installment.amount_remaining_decimal,
            }
            for installment in tax_installment_qs
        ]


class LinkTransactionView(UnfoldModelAdminViewMixin, TemplateView):
    """Link an existing Payment (registered on an invoice) to a BankTransaction."""

    title = _("Link Bank Transaction")
    permission_required = ("banking.change_bankreconciliation",)
    template_name = "admin/banking/link_transaction.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        payment = get_object_or_404(
            Payment.objects.select_related("company"),
            pk=self.kwargs["payment_id"],
        )
        context.update({
            "payment": payment,
            "payment_amount": abs(payment.amount.amount),
            "transactions": self._get_candidate_transactions(payment),
            "back_url": self.request.GET.get("back", "/"),
            "submit_url": reverse(
                "admin:banking_payment_link_transaction",
                args=[payment.pk],
            ),
        })
        return context

    def post(self, request, payment_id):
        payment = get_object_or_404(
            Payment.objects.select_related("company"),
            pk=payment_id,
        )
        back_url = request.POST.get("back", "/")

        txn_pk = request.POST.get("selected_txn")
        if not txn_pk:
            messages.error(request, _("Please select a bank transaction."))
            return redirect(f"{request.path}?back={back_url}")

        txn = get_object_or_404(BankTransaction, pk=txn_pk)

        if txn.reconciliation_status == ReconciliationStatus.RECONCILED:
            messages.warning(request, _("This transaction is already fully reconciled."))
            return redirect(back_url)

        try:
            TransactionMatcher.confirm_existing_payment(txn, payment, request.user)
            messages.success(request, _("Payment linked to bank transaction successfully."))
        except ValueError as e:
            messages.warning(request, str(e))
        except Exception as e:
            logger.exception("Error linking payment %s to transaction %s", payment_id, txn_pk)
            messages.error(request, _("Error: %(error)s") % {"error": str(e)})

        return redirect(back_url)

    @staticmethod
    def _get_candidate_transactions(payment: Payment) -> list:
        """All non-fully-reconciled BankTransactions for this payment's company."""
        qs = BankTransaction.objects.filter(
            company=payment.company,
            reconciliation_status__in=[
                ReconciliationStatus.UNRECONCILED,
                ReconciliationStatus.PARTIAL,
            ],
        ).select_related("bank_account", "reconciliation").order_by("-transaction_date")

        results = []
        for txn in qs:
            if txn.reconciliation_status == ReconciliationStatus.PARTIAL:
                try:
                    effective_amount = abs(txn.reconciliation.unmatched_amount.amount)
                except BankReconciliation.DoesNotExist:
                    effective_amount = abs(txn.amount.amount)
            else:
                effective_amount = abs(txn.amount.amount)

            results.append({
                "pk": txn.pk,
                "date": txn.transaction_date,
                "amount": txn.amount,
                "abs_amount": abs(txn.amount.amount),
                "effective_amount": effective_amount,
                "description": txn.description or "—",
                "counterparty": txn.counterparty_name or "—",
                "bank_account": str(txn.bank_account),
                "is_partial": txn.reconciliation_status == ReconciliationStatus.PARTIAL,
            })
        return results