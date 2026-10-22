"""Payments are booked in the treasury journals; each allocation's strategy books what it settled."""
from datetime import date
from decimal import Decimal

from django.contrib.auth.models import User
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ImproperlyConfigured
from django.db.models import ProtectedError
from django.test import TestCase
from django.urls import reverse
from django.utils import translation
from djmoney.money import Money

from accounting.models import (
    AccountRole, CompanyAccount, CompanyChartOfAccounts, EntryStatus, JournalEntry, JournalType,
)
from accounting.services.journal_generator import JournalizeError, MissingAccount, UnsupportedCurrency
from accounting.services.payment_journal import AllocationStrategy, CounterpartLine, register, registry
from accounting.services.payment_journal.engine import NoStrategy, PaymentJournalService
from accounting.services.setup_check import WARNING, AccountingSetupCheck
from accounting.tests_sales_journal import SalesLedgerMixin
from company.models import BankAccount
from invoices.models import Invoice
from materials.models import Provider
from payments.models import Payment, PaymentAllocation, PaymentDirection, PaymentMethod, PaymentStatus
from sales.models import Client

PAID_ON = date(2026, 10, 1)


class PaymentLedgerMixin(SalesLedgerMixin):
    """The sales ledger plus a bank account, a cash account and a provider."""

    def setUp(self):
        super().setUp()
        self.bank = BankAccount.objects.create(company=self.company, bank_name="Banco Uno", iban="ES0012345678")
        self.cash = CompanyAccount.objects.create(
            company=self.company, code="570000", name="Caja", role=AccountRole.CASH,
        )
        self.provider = Provider.objects.create(company=self.company, name="Perfiles SA", tax_identifier="B1")

    def pay(self, amount, *documents, direction=PaymentDirection.INBOUND, method=PaymentMethod.TRANSFER,
            status=PaymentStatus.CONFIRMED, **fields):
        """A payment of ``amount`` allocated to ``documents``: (document, amount) pairs."""
        fields.setdefault("bank_account", self.bank if method != PaymentMethod.CASH else None)
        fields.setdefault("payment_date", PAID_ON)
        fields.setdefault("reference", "R-1")
        payment = Payment.objects.create(
            company=self.company, amount=Money(amount, "EUR"), direction=direction,
            method=method, status=status, **fields,
        )
        for document, applied in documents:
            self.allocate(payment, document, applied)
        return payment

    def allocate(self, payment, document, amount):
        return PaymentAllocation.objects.create(
            payment=payment, content_type=ContentType.objects.get_for_model(document), object_id=document.pk,
            amount_applied=Money(amount, "EUR"),
        )

    def sync(self, payment):
        return PaymentJournalService(self.company).sync(payment)

    def totals(self, entry):
        """{account code: debit - credit}"""
        totals = {}
        for line in entry.lines.select_related("account"):
            totals[line.account.code] = totals.get(line.account.code, 0) + line.debit.amount - line.credit.amount
        return totals

    def live(self, payment):
        return payment.journal_entries.get(reversal_of__isnull=True, reversal__isnull=True)

    def purchase_invoice(self, total, provider="default", number="P-1", date_issued=PAID_ON):
        """Saved once: saving again recomputes the total from its (absent) lines."""
        return Invoice.objects.create(
            company=self.company, provider=self.provider if provider == "default" else provider,
            invoice_number=number, date_issued=date_issued, total_amount=Money(total, "EUR"),
        )


class PaymentJournalTests(PaymentLedgerMixin, TestCase):
    def test_collection_debits_the_bank_and_settles_the_client(self):
        invoice = self.issue(self.proforma("100.00"))
        payment = self.pay("121.00", (invoice, "121.00"))

        entry = self.sync(payment)

        self.assertEqual(entry.status, EntryStatus.POSTED)
        self.assertEqual(entry.journal.type, JournalType.BANK)
        self.assertEqual(entry.date, PAID_ON)
        self.bank.refresh_from_db()
        self.assertEqual(self.bank.account.code, "5720001")
        self.assertEqual(self.totals(entry), {"5720001": Decimal("121.00"), "4300001": Decimal("-121.00")})

    def test_bank_accounts_are_numbered_under_the_bank_root(self):
        other = BankAccount.objects.create(company=self.company, bank_name="Banco Dos", iban="ES0099990001")
        self.sync(self.pay("10.00", bank_account=self.bank, client=self.client_obj))
        self.sync(self.pay("10.00", bank_account=other, client=self.client_obj))

        other.refresh_from_db()
        self.assertEqual(other.account.code, "5720002")
        self.assertEqual(other.account.name, "Banco Dos 0001")

    def test_payment_to_a_provider_credits_the_bank(self):
        invoice = self.purchase_invoice("50.00")
        payment = self.pay("50.00", (invoice, "50.00"), direction=PaymentDirection.OUTBOUND)

        self.assertEqual(self.totals(self.sync(payment)), {"5720001": Decimal("-50.00"), "4000001": Decimal("50.00")})

    def test_purchase_invoice_without_provider_is_named(self):
        invoice = self.purchase_invoice("50.00", provider=None)
        payment = self.pay("50.00", (invoice, "50.00"), direction=PaymentDirection.OUTBOUND)

        with self.assertRaisesMessage(MissingAccount, "P-1"):
            self.sync(payment)

    def test_cash_goes_to_the_cash_role_in_the_cash_journal(self):
        payment = self.pay("20.00", method=PaymentMethod.CASH, client=self.client_obj)

        entry = self.sync(payment)

        self.assertEqual(entry.journal.type, JournalType.CASH)
        self.assertEqual(self.totals(entry)["570000"], Decimal("20.00"))

    def test_cash_without_the_role_is_named(self):
        self.cash.role = ""
        self.cash.save()

        with self.assertRaisesMessage(MissingAccount, str(AccountRole.CASH.label)):
            self.sync(self.pay("20.00", method=PaymentMethod.CASH, client=self.client_obj))

    def test_transfer_without_a_bank_account_is_refused(self):
        with self.assertRaises(MissingAccount):
            self.sync(self.pay("20.00", bank_account=None, client=self.client_obj))

    def test_unallocated_money_stays_on_the_clients_account(self):
        invoice = self.issue(self.proforma("100.00"))
        payment = self.pay("200.00", (invoice, "121.00"))

        self.assertEqual(self.totals(self.sync(payment)), {"5720001": Decimal("200.00"), "4300001": Decimal("-200.00")})

    def test_applying_the_credit_later_leaves_the_entry_alone(self):
        first, second = self.issue(self.proforma("100.00")), self.issue(self.proforma("50.00"))
        payment = self.pay("200.00", (first, "121.00"))
        entry = self.sync(payment)

        self.allocate(payment, second, "60.50")

        self.assertIsNone(self.sync(payment))
        self.assertEqual(self.live(payment), entry)

    def test_unallocated_money_with_nobody_to_hold_it_is_refused(self):
        with self.assertRaises(MissingAccount):
            self.sync(self.pay("20.00"))

    def test_provider_holds_an_outbound_payment_on_account(self):
        payment = self.pay("30.00", direction=PaymentDirection.OUTBOUND, provider=self.provider)

        self.assertEqual(self.totals(self.sync(payment))["4000001"], Decimal("30.00"))

    def test_syncing_twice_books_once(self):
        payment = self.pay("20.00", client=self.client_obj)
        self.sync(payment)

        self.assertIsNone(self.sync(payment))
        self.assertEqual(payment.journal_entries.count(), 1)

    def test_draft_payment_books_nothing(self):
        payment = self.pay("20.00", client=self.client_obj, status=PaymentStatus.DRAFT)

        self.assertIsNone(self.sync(payment))
        self.assertFalse(payment.journal_entries.exists())

    def test_mirror_allocations_are_not_money(self):
        invoice = self.issue(self.proforma("100.00"))
        payment = self.pay("121.00", (invoice, "121.00"))
        PaymentAllocation.objects.create(
            payment=payment, content_type=ContentType.objects.get_for_model(Client), object_id=self.client_obj.pk,
            amount_applied=Money("121.00", "EUR"), is_mirror=True,
        )

        self.assertEqual(self.totals(self.sync(payment))["4300001"], Decimal("-121.00"))

    def test_foreign_currency_is_refused(self):
        payment = Payment.objects.create(
            company=self.company, payment_date=PAID_ON, amount=Money("10.00", "USD"),
            direction=PaymentDirection.INBOUND, method=PaymentMethod.TRANSFER, status=PaymentStatus.CONFIRMED,
            bank_account=self.bank, client=self.client_obj,
        )

        with self.assertRaises(UnsupportedCurrency):
            self.sync(payment)

    def test_ledger_refusing_to_post_leaves_a_draft(self):
        self.fiscal_year.delete()

        entry = self.sync(self.pay("20.00", client=self.client_obj))

        self.assertEqual(entry.status, EntryStatus.DRAFT)
        self.assertTrue(entry.post_error)


class PaymentRebookingTests(PaymentLedgerMixin, TestCase):
    """A posted entry that no longer matches its payment is reversed and booked again."""

    def setUp(self):
        super().setUp()
        self.payment = self.pay("20.00", client=self.client_obj)
        self.original = self.sync(self.payment)

    def test_changing_the_amount_reverses_and_rebooks(self):
        self.payment.amount = Money("25.00", "EUR")
        self.payment.save()

        entry = self.sync(self.payment)

        self.original.refresh_from_db()
        self.assertEqual(self.original.reversal.status, EntryStatus.POSTED)
        self.assertEqual(self.totals(self.original.reversal)["5720001"], Decimal("-20.00"))
        self.assertEqual(self.totals(entry)["5720001"], Decimal("25.00"))
        self.assertEqual(self.live(self.payment), entry)

    def test_moving_the_date_rebooks(self):
        self.payment.payment_date = date(2026, 10, 5)
        self.payment.save()

        self.assertEqual(self.sync(self.payment).date, date(2026, 10, 5))

    def test_allocating_to_another_partner_rebooks(self):
        other = Client.objects.create(name="Otro SA", company=self.company)
        invoice = self.proforma("10.00")
        invoice.client = other
        invoice.save()
        self.issue(invoice)
        self.allocate(self.payment, invoice, "12.10")

        entry = self.sync(self.payment)

        self.assertEqual(self.totals(entry), {
            "5720001": Decimal("20.00"), "4300001": Decimal("-7.90"), "4300002": Decimal("-12.10"),
        })

    def test_cancelling_only_reverses(self):
        self.payment.status = PaymentStatus.CANCELLED
        self.payment.save()

        self.assertIsNone(self.sync(self.payment))
        self.original.refresh_from_db()
        self.assertEqual(self.original.reversal.status, EntryStatus.POSTED)
        self.assertFalse(
            self.payment.journal_entries.filter(reversal_of__isnull=True, reversal__isnull=True).exists()
        )

    def test_a_draft_is_rebuilt_not_reversed(self):
        JournalEntry.objects.filter(pk=self.original.pk).update(status=EntryStatus.DRAFT, number="")
        self.payment.amount = Money("25.00", "EUR")
        self.payment.save()

        entry = self.sync(self.payment)

        self.assertFalse(JournalEntry.objects.filter(pk=self.original.pk).exists())
        self.assertEqual(self.payment.journal_entries.get(), entry)

    def test_a_failure_leaves_the_ledger_as_it_was(self):
        self.allocate(self.payment, self.client_obj, "5.00")  # no strategy books clients

        with self.assertRaises(NoStrategy):
            self.sync(self.payment)
        self.assertEqual(self.live(self.payment), self.original)

    def test_journalized_payment_cannot_be_deleted(self):
        with self.assertRaises(ProtectedError):
            self.payment.delete()


class PaymentJournalTriggerTests(PaymentLedgerMixin, TestCase):
    def test_journalized_once_on_commit_however_often_it_is_saved(self):
        invoice = self.issue(self.proforma("100.00"))

        with self.captureOnCommitCallbacks(execute=True) as callbacks:
            payment = self.pay("121.00", (invoice, "60.00"), (invoice, "61.00"))

        journalizing = [c for c in callbacks if getattr(c, "payment_journal_id", None) == payment.pk]
        self.assertEqual(len(journalizing), 1)
        self.assertEqual(self.totals(self.live(payment))["4300001"], Decimal("-121.00"))

    def test_company_without_chart_is_left_alone(self):
        CompanyChartOfAccounts.objects.filter(company=self.company).delete()

        with self.captureOnCommitCallbacks(execute=True):
            payment = self.pay("20.00", client=self.client_obj)

        self.assertFalse(payment.journal_entries.exists())

    def test_failure_never_blocks_the_payment(self):
        with self.captureOnCommitCallbacks(execute=True):
            payment = self.pay("20.00")  # nobody to hold it

        self.assertTrue(Payment.objects.filter(pk=payment.pk).exists())
        self.assertFalse(payment.journal_entries.exists())


class AllocationStrategyRegistryTests(TestCase):
    def test_a_model_has_one_strategy(self):
        with self.assertRaises(ImproperlyConfigured):
            @register
            class Again(AllocationStrategy):
                model = Invoice



class StrategyContractTests(PaymentLedgerMixin, TestCase):
    def test_lines_must_add_up_to_the_allocation(self):
        class Short(AllocationStrategy):
            model = Client

            def lines(self, allocation, document, ledger):
                return [CounterpartLine(ledger.client_account(document), "x", Decimal("1.00"))]

        key = Client._meta.label_lower
        registry._strategies[key] = Short()
        self.addCleanup(registry._strategies.pop, key)
        payment = self.pay("20.00", (self.client_obj, "20.00"))

        with self.assertRaisesMessage(JournalizeError, "don't add up"):
            self.sync(payment)


class PaymentAdminTests(PaymentLedgerMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.client.force_login(User.objects.create_superuser("finance", "fin@example.com", "pw"))

    def test_button_reports_the_missing_account(self):
        payment = self.pay("20.00")

        with translation.override("en"):
            response = self.client.get(
                reverse("admin:payments_payment_journalize_detail_action", args=[payment.pk]), follow=True,
            )

        self.assertContains(response, "no client or provider")

    def test_button_posts_and_the_list_links_the_entry(self):
        payment = self.pay("20.00", client=self.client_obj)

        with translation.override("en"):
            self.client.get(reverse("admin:payments_payment_journalize_detail_action", args=[payment.pk]))
            response = self.client.get(reverse("admin:payments_payment_changelist"))

        entry = self.live(payment)
        self.assertEqual(entry.status, EntryStatus.POSTED)
        self.assertContains(response, entry.number)

    def test_setup_check_counts_payments_without_entry(self):
        self.sync(self.pay("20.00", client=self.client_obj))
        self.pay("30.00")  # can't be booked

        without = AccountingSetupCheck(self.company).run()[-1].checks[0]

        self.assertEqual(without.status, WARNING)
        self.assertTrue(without.detail.startswith("1 "))


class ConfiguredAccountTests(PaymentLedgerMixin, TestCase):
    """Documents with no strategy in code, and money with no document, book to accounts set in the admin."""

    def setUp(self):
        super().setUp()
        self.employees = CompanyAccount.objects.create(company=self.company, code="465000", name="Remuneraciones")
        self.fees = CompanyAccount.objects.create(company=self.company, code="626000", name="Comisiones")
        # Nothing in code books payments allocated to a client record: it stands in for any such document
        self.document = self.client_obj

    def rule(self, account):
        from accounting.models import AllocationAccountRule

        return AllocationAccountRule.objects.create(
            company=self.company, content_type=ContentType.objects.get_for_model(self.document), account=account,
        )

    def test_rule_books_the_document_type(self):
        self.rule(self.employees)
        payment = self.pay("40.00", (self.document, "40.00"), direction=PaymentDirection.OUTBOUND)

        self.assertEqual(self.totals(self.sync(payment)), {"5720001": Decimal("-40.00"), "465000": Decimal("40.00")})

    def test_account_on_the_allocation_wins_over_the_rule(self):
        self.rule(self.employees)
        payment = self.pay("40.00", direction=PaymentDirection.OUTBOUND)
        allocation = self.allocate(payment, self.document, "40.00")
        PaymentAllocation.objects.filter(pk=allocation.pk).update(account=self.fees)

        self.assertEqual(self.totals(self.sync(payment))["626000"], Decimal("40.00"))

    def test_without_rule_or_account_it_stays_unbooked(self):
        payment = self.pay("40.00", (self.document, "40.00"))

        with self.assertRaisesMessage(NoStrategy, "payment account rule"):
            self.sync(payment)

    def test_invoices_refuse_an_account_on_the_allocation(self):
        from django.core.exceptions import ValidationError

        invoice = self.issue(self.proforma("100.00"))
        allocation = self.allocate(self.pay("121.00"), invoice, "121.00")
        allocation.account = self.fees

        with self.assertRaises(ValidationError):
            allocation.clean()

    def test_account_on_the_payment_takes_money_with_no_document(self):
        payment = self.pay("3.50", direction=PaymentDirection.OUTBOUND, account=self.fees, client=self.client_obj)

        self.assertEqual(self.totals(self.sync(payment)), {"5720001": Decimal("-3.50"), "626000": Decimal("3.50")})

    def test_bank_category_account_books_a_reconciled_payment_with_no_document(self):
        from banking.models import BankReconciliation, BankTransaction, BankTransactionCategory

        category = BankTransactionCategory.objects.create(company=self.company, name="Comisiones", account=self.fees)
        payment = self.pay("3.50", direction=PaymentDirection.OUTBOUND)
        txn = BankTransaction.objects.create(
            company=self.company, bank_account=self.bank, external_id="fee-1", transaction_date=PAID_ON,
            amount=Money("-3.50", "EUR"), currency="EUR", raw_data={}, category=category,
        )
        BankReconciliation.objects.create(company=self.company, bank_transaction=txn, payment=payment, is_confirmed=True)

        self.assertEqual(self.totals(self.sync(payment))["626000"], Decimal("3.50"))

    def test_client_still_holds_money_on_account_before_the_category(self):
        from banking.models import BankReconciliation, BankTransaction, BankTransactionCategory

        category = BankTransactionCategory.objects.create(company=self.company, name="Cobros", account=self.fees)
        payment = self.pay("20.00", client=self.client_obj)
        txn = BankTransaction.objects.create(
            company=self.company, bank_account=self.bank, external_id="in-1", transaction_date=PAID_ON,
            amount=Money("20.00", "EUR"), currency="EUR", raw_data={}, category=category,
        )
        BankReconciliation.objects.create(company=self.company, bank_transaction=txn, payment=payment, is_confirmed=True)

        self.assertEqual(self.totals(self.sync(payment))["4300001"], Decimal("-20.00"))

    def test_setup_check_lists_document_types_waiting_for_a_rule(self):
        self.sync(self.pay("20.00", client=self.client_obj))
        self.pay("40.00", (self.document, "40.00"))

        checks = AccountingSetupCheck(self.company).run()[-1].checks

        self.assertEqual(checks[0].status, WARNING)
        self.assertTrue(checks[0].detail.startswith("1 "))

    def test_rule_admin_offers_only_documents_without_a_strategy(self):
        from accounting.admin.allocation_rules import configurable_document_types
        from apps.expenses.models import ExpenseClaim
        from invoices.models import SalesInvoice

        models = {content_type.model_class() for content_type in configurable_document_types()}

        self.assertIn(ExpenseClaim, models)
        self.assertNotIn(SalesInvoice, models)
        self.assertNotIn(Invoice, models)


class ReconcileToCategoryTests(PaymentLedgerMixin, TestCase):
    """A bank movement no document backs is reconciled straight to its category's account."""

    def setUp(self):
        super().setUp()
        from banking.models import BankTransaction, BankTransactionCategory

        self.fees = CompanyAccount.objects.create(company=self.company, code="626000", name="Servicios bancarios")
        self.category = BankTransactionCategory.objects.create(
            company=self.company, name="Comisiones bancarias", account=self.fees,
        )
        self.txn = BankTransaction.objects.create(
            company=self.company, bank_account=self.bank, external_id="plan-fee", transaction_date=PAID_ON,
            amount=Money("-35.00", "EUR"), currency="EUR", raw_data={"description": "Company Pro plan fee"},
        )
        self.client.force_login(User.objects.create_superuser("finance", "fin@example.com", "pw"))

    def post(self, category):
        with translation.override("en"), self.captureOnCommitCallbacks(execute=True):
            return self.client.post(
                reverse("admin:banking_banktransaction_reconcile_category", args=[self.txn.pk]),
                {"category": category.pk}, follow=True,
            )

    def test_books_the_fee_to_the_category_account(self):
        from banking.models import ReconciliationStatus

        response = self.post(self.category)

        self.assertContains(response, "626000")
        self.txn.refresh_from_db()
        self.assertEqual(self.txn.reconciliation_status, ReconciliationStatus.RECONCILED)
        self.assertEqual(self.txn.category, self.category)
        reconciliation = self.txn.reconciliation
        self.assertTrue(reconciliation.is_confirmed)
        self.assertEqual(reconciliation.unmatched_amount.amount, 0)
        payment = reconciliation.payment
        self.assertEqual(payment.direction, PaymentDirection.OUTBOUND)
        self.assertFalse(payment.allocations.exists())
        entry = self.live(payment)
        self.assertEqual(entry.status, EntryStatus.POSTED)
        self.assertEqual(self.totals(entry), {"5720001": Decimal("-35.00"), "626000": Decimal("35.00")})

    def test_category_without_account_is_refused(self):
        from banking.models import BankTransactionCategory, ReconciliationStatus

        bare = BankTransactionCategory.objects.create(company=self.company, name="Varios")

        response = self.post(bare)

        self.assertContains(response, "has no accounting account")
        self.txn.refresh_from_db()
        self.assertEqual(self.txn.reconciliation_status, ReconciliationStatus.UNRECONCILED)
        self.assertFalse(Payment.objects.filter(company=self.company).exists())

    def test_reconciled_transaction_is_refused(self):
        self.post(self.category)

        response = self.post(self.category)

        self.assertContains(response, "Only unreconciled transactions")
        self.assertEqual(Payment.objects.filter(company=self.company).count(), 1)

    def test_screen_offers_only_categories_with_an_account(self):
        from banking.models import BankTransactionCategory

        BankTransactionCategory.objects.create(company=self.company, name="Sin cuenta")

        with translation.override("en"):
            response = self.client.get(reverse("admin:banking_banktransaction_manual_reconcile", args=[self.txn.pk]))

        self.assertContains(response, "Comisiones bancarias · 626000 Servicios bancarios")
        self.assertNotContains(response, "Sin cuenta")

    def test_documents_need_no_category(self):
        from banking.models import BankTransaction, ReconciliationStatus

        invoice = self.issue(self.proforma("100.00"))
        txn = BankTransaction.objects.create(
            company=self.company, bank_account=self.bank, external_id="cobro-1", transaction_date=PAID_ON,
            amount=Money("121.00", "EUR"), currency="EUR", raw_data={},
        )

        with translation.override("en"), self.captureOnCommitCallbacks(execute=True):
            self.client.post(
                reverse("admin:banking_banktransaction_manual_reconcile", args=[txn.pk]),
                {"selected_docs": [f"salesinvoice:{invoice.pk}"]},
            )

        txn.refresh_from_db()
        self.assertEqual(txn.reconciliation_status, ReconciliationStatus.RECONCILED)
        self.assertIsNone(txn.category)
        self.assertEqual(self.totals(self.live(txn.reconciliation.payment))["4300001"], Decimal("-121.00"))

    def test_no_document_and_no_category_is_refused(self):
        from banking.models import BankReconciliation
        from banking.services.transaction_matching import TransactionMatcher

        reconciliation = BankReconciliation.objects.create(bank_transaction=self.txn, is_confirmed=False)

        with self.assertRaises(ValueError):
            TransactionMatcher.confirm(reconciliation, [], None)
        self.assertFalse(Payment.objects.filter(company=self.company).exists())
