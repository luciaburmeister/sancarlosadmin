import logging
from decimal import Decimal

from django import forms
from django.apps import apps
from django.contrib import messages
from django.contrib.contenttypes.models import ContentType
from django.db import transaction as db_transaction
from django.db.models import Sum
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils import timezone
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
from banking.services.transaction_matching import TransactionMatcher
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

                docs.append({
                    "content_type": alloc["content_type"],
                    "object_id": doc.pk,
                    "number": getattr(doc, "invoice_number", str(doc)),
                    "date": getattr(doc, "date_issued", None),
                    "counterparty": str(
                        getattr(doc, "client", None)
                        or getattr(doc, "provider", None)
                        or "—"
                    ),
                    "remaining": remaining,
                    "change_url": change_url,
                })

            combinations.append({
                "index": idx,
                "is_selected": combo.get("is_selected", False),
                "confidence": combo.get("confidence", "none"),
                "docs": docs,
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

        if category is None:
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


class ManualReconcileView(UnfoldModelAdminViewMixin, TemplateView):
    """Manual reconciliation — user picks invoices directly without the matcher."""

    title = _("Reconcile Manually")
    permission_required = ("banking.change_banktransaction",)
    template_name = "admin/banking/manual_reconcile.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        txn = get_object_or_404(
            BankTransaction.objects.select_related("bank_account", "company"),
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

        context.update({
            "txn": txn,
            "txn_amount": effective_amount,
            "is_partial": is_partial,
            "documents": documents,
            "categories": BankTransactionCategory.objects.filter(
                company=txn.company
            ).order_by("name"),
            "submit_url": reverse(
                "admin:banking_banktransaction_manual_reconcile",
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

        if category is None:
            messages.error(
                request,
                _("Please select a category before confirming this reconciliation."),
            )
            return redirect(reverse("admin:banking_banktransaction_manual_reconcile", args=[pk]))

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
        approved expense claims for this transaction's company."""
        Invoice = apps.get_model("invoices", "Invoice")
        SalesInvoice = apps.get_model("invoices", "SalesInvoice")
        LoanInstallment = apps.get_model("loans", "LoanInstallment")
        ExpenseClaim = apps.get_model("expenses", "ExpenseClaim")

        direction = PaymentDirection.INBOUND if txn.amount.amount > 0 else PaymentDirection.OUTBOUND
        results = []

        if direction == PaymentDirection.OUTBOUND:
            qs = Invoice.objects.filter(
                company=txn.company,
                payment_status__in=["unpaid", "partial"],
                date_issued__lte=txn.transaction_date,
            ).select_related("provider").order_by("-date_issued")

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

            installment_qs = LoanInstallment.objects.filter(
                loan__company=txn.company,
                loan__bank_account=txn.bank_account,
                payment_status__in=["unpaid", "partial"],
                due_date__lte=txn.transaction_date,
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

            from apps.expenses.models import ExpenseClaimStatus
            claim_qs = ExpenseClaim.objects.filter(
                company=txn.company,
                status=ExpenseClaimStatus.APPROVED,
                created_at__date__lte=txn.transaction_date,
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
            qs = SalesInvoice.objects.filter(
                company=txn.company,
                payment_status__in=["unpaid", "partial"],
                date_issued__lte=txn.transaction_date,
            ).select_related("client").order_by("-date_issued")

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

        return results


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
            payment.reconciliation  # raises DoesNotExist if not yet linked
            messages.warning(request, _("This payment is already linked to a bank transaction."))
            return redirect(back_url)
        except BankReconciliation.DoesNotExist:
            pass

        try:
            with db_transaction.atomic():
                total_allocated = (
                    payment.allocations.aggregate(total=Sum("amount_applied"))["total"]
                    or Decimal("0")
                )
                txn_amount = abs(txn.amount.amount)
                unmatched = max(Decimal("0"), txn_amount - total_allocated)

                BankReconciliation.objects.create(
                    bank_transaction=txn,
                    payment=payment,
                    is_confirmed=True,
                    confirmed_at=timezone.now(),
                    reconciled_by=request.user,
                    method=ReconciliationMethod.MANUAL,
                    suggested_allocations=[],
                    unmatched_amount=Money(unmatched, txn.amount_currency),
                )

                txn.reconciliation_status = (
                    ReconciliationStatus.RECONCILED
                    if unmatched <= Decimal("0.01")
                    else ReconciliationStatus.PARTIAL
                )
                txn.save(update_fields=["reconciliation_status"])

            messages.success(request, _("Payment linked to bank transaction successfully."))
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