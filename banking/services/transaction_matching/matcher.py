"""Main orchestrator for bank transaction matching and reconciliation."""
from datetime import timedelta
from decimal import Decimal

from django.apps import apps
from django.contrib.contenttypes.models import ContentType
from django.db import transaction as db_transaction
from django.db.models import Sum
from django.utils import timezone
from django.utils.translation import gettext as _
from djmoney.money import Money

from banking.models import (
    BankReconciliation,
    BankTransaction,
    BankTransactionCategory,
    ReconciliationMethod,
    ReconciliationStatus,
)
from banking.services.transaction_matching import candidates, direct_debit, reference
from banking.services.transaction_matching.amount_matching import _remaining, match_amount
from banking.services.transaction_matching.counterparty import (
    find_counterparty,
    find_employee_counterparty,
)
from banking.services.transaction_matching.types import (
    PAYMENT_DATE_TOLERANCE_DAYS,
    MatchResult,
)
from payments.mixins import annotate_amount_remaining
from payments.models import (
    Payment,
    PaymentAllocation,
    PaymentCreatedBy,
    PaymentDirection,
    PaymentMethod,
    PaymentStatus,
    PromissoryNote,
)


class TransactionMatcher:
    """
    Orchestrates matching a BankTransaction to unpaid documents.

    Usage:
        matcher = TransactionMatcher(bank_transaction)
        reconciliation = matcher.run()
    """

    def __init__(self, bank_transaction: BankTransaction):
        self.transaction = bank_transaction
        self.direction = self._get_direction()
        self.counterparty = None
        self.employee_counterparty = None
        self.result: MatchResult | None = None

    def run(self) -> BankReconciliation:
        """
        Run the matching pipeline and create a BankReconciliation suggestion.
        Never creates a Payment — that only happens on confirmation (human,
        or auto_confirm).

        Invoices whose payment reference is in the concept come first and
        selected; with none, so does the one invoice a direct debit charge
        collects (its exact amount, on its due date). Amount
        matching still runs and fills the alternatives.
        A payment already registered for this movement (it expects the
        reference in the concept) goes before them all: the movement is that
        payment, so confirming links it instead of creating one.
        """
        referenced = reference.find_referenced_documents(self.transaction, self.direction)
        self.counterparty = (
            referenced and reference.counterparty_of(referenced)
        ) or find_counterparty(self.transaction, self.direction)
        pinned, pinned_confidence = referenced, "reference"
        if not referenced:
            charged = direct_debit.find_charged_invoice(self.transaction)
            pinned, pinned_confidence = ([charged], "direct_debit") if charged else ([], None)
        candidate_documents = self._get_candidate_documents()
        self.result = match_amount(
            self.transaction,
            candidate_documents,
            has_counterparty=self.counterparty is not None or self.employee_counterparty is not None,
        )

        if not self.result.has_match:
            oldest_first_match = self._tax_installment_oldest_first_match(candidate_documents)
            if oldest_first_match:
                self.result = MatchResult(documents=oldest_first_match, confidence="oldest_first")

        # Build the combined suggested_allocations structure:
        # [{"allocations": [...], "confidence": "exact", "is_selected": bool}, ...]
        all_combos = [self.result.documents] + self.result.alternatives
        suggested_allocations = [
            {
                "allocations": self._serialize_docs(docs),
                "confidence": self.result.confidence,
                "is_selected": idx == 0 and self.result.has_match and not pinned,
            }
            for idx, docs in enumerate(all_combos)
            if docs  # skip empty
        ]
        unmatched_amount = self.result.unmatched_amount

        if pinned:
            pinned_allocations = self._serialize_docs(pinned)
            suggested_allocations = [
                {
                    "allocations": pinned_allocations,
                    "confidence": pinned_confidence,
                    "is_selected": True,
                },
                *(combo for combo in suggested_allocations if combo["allocations"] != pinned_allocations),
            ]
            remaining = sum((_remaining(doc) for doc in pinned), Decimal("0"))
            unmatched_amount = max(Decimal("0"), abs(self.transaction.amount.amount) - remaining)

        payment_combos, selected_payment = self._existing_payment_combos(
            selectable=not any(combo["is_selected"] for combo in suggested_allocations),
        )
        if payment_combos:
            if payment_combos[0]["confidence"] == "payment":
                for combo in suggested_allocations:
                    combo["is_selected"] = False
            suggested_allocations = payment_combos + suggested_allocations
        if selected_payment:
            unmatched_amount = max(Decimal("0"), abs(self.transaction.amount.amount) - selected_payment.amount.amount)

        reconciliation = BankReconciliation.objects.create(
            bank_transaction=self.transaction,
            payment=None,
            is_confirmed=False,
            method=ReconciliationMethod.AUTO,
            suggested_allocations=suggested_allocations,
            unmatched_amount=Money(
                unmatched_amount,
                self.transaction.amount_currency,
            ),
        )

        return reconciliation

    @staticmethod
    def auto_confirm(reconciliation: BankReconciliation) -> bool:
        """
        Confirm a suggestion with no human when the payer named the invoices
        by payment reference, whatever the amount: less than owed pays them
        partially (in the order written); more settles them and leaves the
        excess on the payment as the client's/provider's credit. A direct
        debit charge is confirmed only while its invoice still owes exactly
        the amount charged. Returns whether it confirmed.
        """
        selected = next(
            (combo for combo in reconciliation.suggested_allocations if combo.get("is_selected")),
            None,
        )
        if selected and selected.get("confidence") == "payment":
            # The payment registered for this movement, if it is still for its amount
            payment = Payment.objects.filter(pk=selected["payment_id"]).first()
            txn = reconciliation.bank_transaction
            if not payment or payment.amount.amount != abs(txn.amount.amount):
                return False
            TransactionMatcher.confirm_existing_payment(txn, payment, user=None, method=ReconciliationMethod.AUTO)
            return True
        if selected and selected.get("confidence") == "direct_debit":
            [allocation] = selected["allocations"]
            invoice = apps.get_model("invoices", "Invoice").objects.filter(pk=allocation["object_id"]).first()
            if not invoice or invoice.amount_remaining_decimal != abs(reconciliation.bank_transaction.amount.amount):
                return False
            TransactionMatcher.confirm(reconciliation, selected["allocations"], user=None)
            return True
        if not selected or selected.get("confidence") != "reference":
            return False
        # Paid off since the suggestion was made: nothing left for the money to go to
        if not any(
            ContentType.objects.get(model=allocation["content_type"])
            .get_object_for_this_type(pk=allocation["object_id"]).amount_remaining_decimal > 0
            for allocation in selected["allocations"]
        ):
            return False

        TransactionMatcher.confirm(reconciliation, selected["allocations"], user=None, excess_as_credit=True)
        return True

    @staticmethod
    def _serialize_docs(docs: list) -> list:
        """Convert a list of model instances to the allocation JSON format."""
        return [
            {
                "content_type": ContentType.objects.get_for_model(doc).model,
                "object_id": doc.pk,
            }
            for doc in docs
        ]

    @staticmethod
    def get_selected_allocations(suggested_allocations: list) -> list:
        """Extract the allocations list from the selected combination."""
        for combo in suggested_allocations:
            if combo.get("is_selected"):
                return combo.get("allocations", [])
        return []

    @staticmethod
    @db_transaction.atomic
    def confirm(
        reconciliation: BankReconciliation,
        confirmed_allocations: list,
        user,
        category: BankTransactionCategory | None = None,
        excess_as_credit: bool = False,
    ) -> BankReconciliation:
        """
        Confirm (or modify) a matcher suggestion and create the Payment.

        With `excess_as_credit`, money left over after the documents are paid
        is not "unmatched": it stays on the payment, which took the documents'
        client/provider, as their credit (saldo a favor), so the movement is
        fully reconciled.

        confirmed_allocations is a list of dicts:
            [{"content_type": "invoice", "object_id": 5}, ...]

        If `category` is given, it's assigned to the bank transaction so
        confirming a reconciliation (manual or automatic) also categorizes
        it. Leaving it None keeps whatever category the transaction already
        had. It's only required with no documents: then the category's
        account is what the payment is booked to.

        Idempotent — if the reconciliation is already confirmed, returns it.
        """
        if reconciliation.is_confirmed:
            return reconciliation

        txn = reconciliation.bank_transaction

        if not confirmed_allocations and category is None and txn.category_id is None:
            raise ValueError(_("A movement with no document needs a category to be booked to."))

        # Step 1 — Did the human change the suggestion?
        method = TransactionMatcher._determine_method(
            reconciliation.suggested_allocations, confirmed_allocations,
        )

        # Step 1 — Determine payment direction
        direction = (
            PaymentDirection.INBOUND
            if txn.amount.amount > 0
            else PaymentDirection.OUTBOUND
        )

        payment = Payment.objects.create(
            company=txn.company,
            payment_date=txn.transaction_date,
            amount=abs(txn.amount.amount),
            amount_currency=txn.amount_currency,
            direction=direction,
            bank_account=txn.bank_account,
            created_by=PaymentCreatedBy.AUTO,
            method=PaymentMethod.TRANSFER,
            status=PaymentStatus.CONFIRMED,
        )

        # Step 3 — Create PaymentAllocations
        #   Check the real-time remaining amount at confirmation time.
        #   Another reconciliation may have been confirmed in between,
        #   reducing or zeroing the remaining balance.
        #   Each allocation is capped at what's left of the transaction's
        #   own amount, so a document whose remaining balance is larger
        #   than the transaction (e.g. an invoice paid in installments)
        #   gets partially applied instead of over-allocated.
        txn_amount = abs(txn.amount.amount)
        total_allocated = Decimal("0")
        for allocation in confirmed_allocations:
            budget = txn_amount - total_allocated
            if budget <= 0:
                break
            ct = ContentType.objects.get(model=allocation["content_type"])
            doc = ct.get_object_for_this_type(pk=allocation["object_id"])
            remaining = doc.amount_remaining_decimal
            if remaining <= 0:
                continue  # Already fully paid by another reconciliation
            amount_to_apply = min(remaining, budget)
            PaymentAllocation.objects.create(
                payment=payment,
                content_type=ct,
                object_id=allocation["object_id"],
                amount_applied=amount_to_apply,
            )
            total_allocated += amount_to_apply

        # Step 4 — Update the BankReconciliation
        actual_unmatched = max(Decimal("0"), txn_amount - total_allocated)
        if excess_as_credit and total_allocated > 0:
            payment.refresh_from_db(fields=["client", "provider"])
            if payment.client_id or payment.provider_id:
                actual_unmatched = Decimal("0")
        reconciliation.payment = payment
        reconciliation.is_confirmed = True
        reconciliation.method = method
        reconciliation.reconciled_by = user
        reconciliation.confirmed_at = timezone.now()
        reconciliation.unmatched_amount = Money(actual_unmatched, txn.amount_currency)
        reconciliation.save()

        # Step 5 — Update BankTransaction reconciliation status (+ category)
        is_partial = confirmed_allocations and actual_unmatched > 0
        txn.reconciliation_status = (
            ReconciliationStatus.PARTIAL if is_partial else ReconciliationStatus.RECONCILED
        )
        update_fields = ["reconciliation_status"]
        if category is not None:
            txn.category = category
            update_fields.append("category")
        txn.save(update_fields=update_fields)

        return reconciliation

    @staticmethod
    @db_transaction.atomic
    def confirm_existing_payment(
        txn: BankTransaction,
        payment: Payment,
        user,
        method: str = ReconciliationMethod.MANUAL,
        category: BankTransactionCategory | None = None,
    ) -> BankReconciliation:
        """
        Reconcile ``txn`` with a payment registered before the movement
        reached the bank, instead of creating one: the movement's
        suggestion, if any, becomes the confirmed reconciliation. What the
        payment didn't allocate is left unmatched (PARTIAL).
        """
        if payment.company_id != txn.company_id:
            raise ValueError(_("The payment belongs to another company."))
        if payment.method == PaymentMethod.INTERCOMPANY:
            raise ValueError(_("No money moved in this payment: another company paid, from its own bank."))
        if payment.method == PaymentMethod.COMPENSATION:
            raise ValueError(_("No money moved in this payment: a credit note offset the invoice."))
        if BankReconciliation.objects.filter(payment=payment).exists():
            raise ValueError(_("This payment is already linked to a bank transaction."))
        reconciliation = BankReconciliation.objects.select_for_update().filter(bank_transaction=txn).first()
        if reconciliation and reconciliation.is_confirmed:
            raise ValueError(_("This transaction is already reconciled."))
        if reconciliation is None:
            reconciliation = BankReconciliation(bank_transaction=txn, suggested_allocations=[])

        allocated = (
            payment.allocations.exclude(is_mirror=True).aggregate(total=Sum("amount_applied"))["total"]
            or Decimal("0")
        )
        unmatched = max(Decimal("0"), abs(txn.amount.amount) - allocated)
        reconciliation.payment = payment
        reconciliation.is_confirmed = True
        reconciliation.method = method
        reconciliation.reconciled_by = user
        reconciliation.confirmed_at = timezone.now()
        reconciliation.unmatched_amount = Money(unmatched, txn.amount_currency)
        reconciliation.save()

        txn.reconciliation_status = (
            ReconciliationStatus.RECONCILED if unmatched <= Decimal("0.01") else ReconciliationStatus.PARTIAL
        )
        update_fields = ["reconciliation_status"]
        if category is not None:
            txn.category = category
            update_fields.append("category")
        txn.save(update_fields=update_fields)
        return reconciliation

    @staticmethod
    @db_transaction.atomic
    def confirm_without_documents(
        txn: BankTransaction,
        category: BankTransactionCategory,
        user,
    ) -> BankReconciliation:
        """
        Reconcile a movement no document will ever back (bank fees, interest,
        transfers between own accounts): the whole amount becomes a payment
        with nothing allocated, which the ledger books to the category's
        account. A category without an account is refused, or the payment
        could never be journalized.
        """
        if not category.account_id:
            raise ValueError(_("“%(category)s” has no accounting account: set one on the category first.")
                             % {"category": category.name})
        if txn.reconciliation_status != ReconciliationStatus.UNRECONCILED:
            raise ValueError(_("Only unreconciled transactions can be booked to a category."))

        BankReconciliation.objects.filter(bank_transaction=txn, is_confirmed=False).delete()
        reconciliation = BankReconciliation.objects.create(
            bank_transaction=txn,
            is_confirmed=False,
            method=ReconciliationMethod.MANUAL,
            suggested_allocations=[],
            unmatched_amount=Money(0, txn.amount_currency),
        )
        TransactionMatcher.confirm(reconciliation, [], user, category=category)
        # Nothing is left to match: the category accounts for all of it
        reconciliation.unmatched_amount = Money(0, txn.amount_currency)
        reconciliation.save(update_fields=["unmatched_amount"])
        return reconciliation

    # ------------------------------------------------------------------
    # Private methods
    # ------------------------------------------------------------------

    def _existing_payment_combos(self, selectable: bool) -> tuple[list, Payment | None]:
        """
        Suggestions linking the movement to a payment registered for it, and
        the one selected. One expecting the concept's reference is selected
        (and auto-confirmed on its exact amount); one only named by its
        invoice number is selected when nothing else is, and never confirmed
        on its own.
        """
        expected = reference.find_expected_payments(self.transaction, self.direction)
        if expected:
            payments, confidence, selected = expected, "payment", expected[0]
        else:
            payments = reference.find_payments_named_in_concept(self.transaction, self.direction)
            confidence = "payment_named"
            selected = payments[0] if payments and selectable else None
        combos = [
            {
                "payment_id": payment.pk,
                "allocations": [],
                "confidence": confidence,
                "is_selected": payment == selected,
            }
            for payment in payments
        ]
        return combos, selected

    def _get_direction(self) -> str:
        """Infer payment direction from the transaction amount sign."""
        if self.transaction.amount.amount > 0:
            return PaymentDirection.INBOUND
        return PaymentDirection.OUTBOUND

    def _get_candidate_documents(self) -> list:
        """
        Fetch unpaid / partially paid documents that could match.
        Scoped to the identified counterparty when available.
        Only considers invoices issued on or before the transaction date,
        allowing for a payment to arrive up to PAYMENT_DATE_TOLERANCE_DAYS
        before the document's date (early payments).
        """
        Invoice = apps.get_model("invoices", "Invoice")
        SalesInvoice = apps.get_model("invoices", "SalesInvoice")
        LoanEvent = apps.get_model("loans", "LoanEvent")
        txn_date = self.transaction.transaction_date
        cutoff_date = txn_date + timedelta(days=PAYMENT_DATE_TOLERANCE_DAYS)

        # Querysets whose remaining amount is a SUM over PaymentAllocation
        # are annotated with db_amount_remaining (read by match_amount) so
        # the matcher doesn't run one aggregate per candidate.
        if self.direction == PaymentDirection.OUTBOUND:
            qs = annotate_amount_remaining(
                candidates.invoice_candidates(self.transaction.company, cutoff_date, self.counterparty),
                Invoice,
            )

            installment_qs = candidates.loan_installment_candidates(
                self.transaction.company, self.transaction.bank_account, cutoff_date
            ).select_related("loan")

            event_qs = candidates.loan_event_candidates(
                self.transaction.company, self.transaction.bank_account, cutoff_date
            ).select_related("parent_loan")
            event_qs = annotate_amount_remaining(event_qs, LoanEvent, amount_field="amount")

            # Approved expense claims go to an employee, not a provider, so
            # counterparty resolution is separate from self.counterparty
            # (which is Provider/Client-typed).
            employee = find_employee_counterparty(self.transaction, self.transaction.company)
            self.employee_counterparty = employee
            claim_qs = candidates.expense_claim_candidates(self.transaction.company, cutoff_date, employee)

            payroll_qs = candidates.employee_payroll_candidates(self.transaction.company, cutoff_date)

            tax_installment_qs = candidates.tax_installment_candidates(
                self.transaction.company, cutoff_date
            ).select_related("planned_tax_payment")

            note_qs = candidates.promissory_note_payable_candidates(
                self.transaction.company, self.transaction.bank_account, cutoff_date
            )
            note_qs = annotate_amount_remaining(note_qs, PromissoryNote, amount_field="amount")

            return (
                list(qs) + list(installment_qs) + list(event_qs)
                + list(claim_qs) + list(payroll_qs) + tax_payments + list(note_qs)
            )

        qs = candidates.sales_invoice_candidates(self.transaction.company, cutoff_date, self.counterparty)

        tax_refund_qs = candidates.tax_installment_candidates(
            self.transaction.company, cutoff_date, refund=True
        ).select_related("planned_tax_payment")

        return list(annotate_amount_remaining(qs, SalesInvoice)) + list(tax_refund_qs)

    def _tax_installment_oldest_first_match(self, candidate_documents: list) -> list | None:
        """
        Fallback for a lump payment that doesn't line up exactly with any
        combination of tax installments: pay the oldest due installment in
        full, then apply whatever is left to the next oldest, and so on.
        Only proposed when it fully accounts for the transaction amount —
        confirm() later applies this same oldest-first capping per document.
        """
        TaxInstallment = apps.get_model("finance", "TaxInstallment")
        installments = sorted(
            (doc for doc in candidate_documents if isinstance(doc, TaxInstallment)),
            key=lambda installment: installment.due_date,
        )
        if not installments:
            return None

        budget = abs(self.transaction.amount.amount)
        matched = []
        for installment in installments:
            if budget <= 0:
                break
            remaining = installment.amount_remaining_decimal
            if remaining <= 0:
                continue
            matched.append(installment)
            budget -= remaining

        note_qs = candidates.promissory_note_receivable_candidates(
            self.transaction.company, self.transaction.bank_account, cutoff_date
        )
        note_qs = annotate_amount_remaining(note_qs, PromissoryNote, amount_field="amount")

        return list(annotate_amount_remaining(qs, SalesInvoice)) + tax_refunds + list(note_qs)

    @staticmethod
    def _determine_method(
        suggested_allocations: list,
        confirmed_allocations: list,
    ) -> str:
        """
        Compare what the matcher suggested vs what the human confirmed.
        Same → AUTO; different → MANUAL.
        """
        selected = TransactionMatcher.get_selected_allocations(suggested_allocations)
        if not selected:
            return ReconciliationMethod.MANUAL

        def as_set(allocs):
            return frozenset(
                (a["content_type"], int(a["object_id"]))
                for a in allocs
            )

        if as_set(selected) == as_set(confirmed_allocations):
            return ReconciliationMethod.AUTO
        return ReconciliationMethod.MANUAL