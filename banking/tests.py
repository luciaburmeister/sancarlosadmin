from datetime import date
from decimal import Decimal

from django.test import TestCase
from djmoney.money import Money

from banking.models import BankTransaction, ReconciliationStatus
from banking.services.arcopay_import import ArcopayService
from company.models import BankAccount, Company


class UpdateAccountBalanceTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Test Co")
        self.account = BankAccount.objects.create(
            company=self.company,
            bank_name="Test Bank",
            iban="ES9121000418450200051332",
        )

    def _make_txn(self, ext_id, txn_date, amount, balance):
        raw = {"amount": float(amount), "date": txn_date.strftime("%d-%m-%Y")}
        if balance is not None:
            raw["balance"] = float(balance)
        return BankTransaction.objects.create(
            company=self.company,
            bank_account=self.account,
            external_id=ext_id,
            transaction_date=txn_date,
            amount=Money(amount, "EUR"),
            currency="EUR",
            raw_data=raw,
            reconciliation_status=ReconciliationStatus.UNRECONCILED,
        )

    def test_uses_running_balance_of_latest_transaction_not_amount(self):
        self._make_txn("t1", date(2026, 6, 10), Decimal("100.00"), Decimal("5603.39"))
        self._make_txn("t2", date(2026, 6, 11), Decimal("238.45"), Decimal("5841.84"))

        ArcopayService._update_account_balance(self.account)

        self.account.refresh_from_db()
        self.assertEqual(self.account.last_known_balance, Money("5841.84", "EUR"))
        self.assertIsNotNone(self.account.last_balance_updated_at)

    def test_skips_transactions_without_balance(self):
        self._make_txn("t1", date(2026, 6, 10), Decimal("100.00"), Decimal("5603.39"))
        self._make_txn("t2", date(2026, 6, 11), Decimal("238.45"), None)

        ArcopayService._update_account_balance(self.account)

        self.account.refresh_from_db()
        self.assertEqual(self.account.last_known_balance, Money("5603.39", "EUR"))

    def test_no_update_when_no_transaction_has_balance(self):
        self._make_txn("t1", date(2026, 6, 10), Decimal("100.00"), None)

        ArcopayService._update_account_balance(self.account)

        self.account.refresh_from_db()
        self.assertIsNone(self.account.last_balance_updated_at)
