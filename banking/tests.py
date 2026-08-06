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

    def test_same_day_newest_first_api_order(self):
        # Arcopay returned the day newest-first, so the closing transaction
        # was imported FIRST. The import-order tiebreak would wrongly pick
        # the oldest transaction (balance 2951.90).
        day = date(2026, 6, 11)
        self._make_txn("t1", day, Decimal("429.00"), Decimal("1776.36"))
        self._make_txn("t2", day, Decimal("-1604.54"), Decimal("1347.36"))
        self._make_txn("t3", day, Decimal("-5200.00"), Decimal("2951.90"))

        ArcopayService._update_account_balance(self.account)

        self.account.refresh_from_db()
        self.assertEqual(self.account.last_known_balance, Money("1776.36", "EUR"))

    def test_same_day_oldest_first_api_order(self):
        # Same day returned oldest-first: the closing transaction was
        # imported last. Must still resolve to the same closing balance.
        day = date(2026, 6, 11)
        self._make_txn("t1", day, Decimal("-5200.00"), Decimal("2951.90"))
        self._make_txn("t2", day, Decimal("-1604.54"), Decimal("1347.36"))
        self._make_txn("t3", day, Decimal("429.00"), Decimal("1776.36"))

        ArcopayService._update_account_balance(self.account)

        self.account.refresh_from_db()
        self.assertEqual(self.account.last_known_balance, Money("1776.36", "EUR"))

    def test_same_day_scrambled_api_order(self):
        # Real-world payload where the API order was neither newest- nor
        # oldest-first. True order: t3, t4, t1, t2 → closing 34281.76.
        day = date(2026, 6, 8)
        self._make_txn("t1", day, Decimal("-169.83"), Decimal("34337.27"))
        self._make_txn("t2", day, Decimal("-55.51"), Decimal("34281.76"))
        self._make_txn("t3", day, Decimal("-13373.33"), Decimal("34689.01"))
        self._make_txn("t4", day, Decimal("-181.91"), Decimal("34507.10"))

        ArcopayService._update_account_balance(self.account)

        self.account.refresh_from_db()
        self.assertEqual(self.account.last_known_balance, Money("34281.76", "EUR"))

    def test_same_day_split_across_two_syncs(self):
        # Morning sync imported the first half of the day, afternoon sync
        # the rest (each batch newest-first). The most recently imported
        # transaction (t8) is NOT the closing one.
        day = date(2026, 6, 11)
        self._make_txn("t1", day, Decimal("-658.76"), Decimal("8151.90"))
        self._make_txn("t2", day, Decimal("-300.00"), Decimal("8810.66"))
        self._make_txn("t3", day, Decimal("-545.56"), Decimal("9110.66"))
        self._make_txn("t4", day, Decimal("-844.06"), Decimal("9656.22"))
        self._make_txn("t5", day, Decimal("-2873.25"), Decimal("10500.28"))
        self._make_txn("t6", day, Decimal("429.00"), Decimal("1776.36"))
        self._make_txn("t7", day, Decimal("-1604.54"), Decimal("1347.36"))
        self._make_txn("t8", day, Decimal("-5200.00"), Decimal("2951.90"))

        ArcopayService._update_account_balance(self.account)

        self.account.refresh_from_db()
        self.assertEqual(self.account.last_known_balance, Money("1776.36", "EUR"))

    def test_ambiguous_chain_falls_back_to_last_imported(self):
        # +100 then -100 produces a self-cancelling chain that cannot be
        # ordered by balances alone → fall back to the last imported txn.
        day = date(2026, 6, 11)
        self._make_txn("t1", day, Decimal("100.00"), Decimal("1100.00"))
        self._make_txn("t2", day, Decimal("-100.00"), Decimal("1000.00"))

        with self.assertLogs("banking.services.arcopay_import", level="WARNING"):
            ArcopayService._update_account_balance(self.account)

        self.account.refresh_from_db()
        self.assertEqual(self.account.last_known_balance, Money("1000.00", "EUR"))

    def test_older_date_order_does_not_affect_result(self):
        # Multi-transaction older day plus a single newer transaction:
        # only the newest date matters.
        self._make_txn("t1", date(2026, 6, 10), Decimal("100.00"), Decimal("5603.39"))
        self._make_txn("t2", date(2026, 6, 10), Decimal("-50.00"), Decimal("5553.39"))
        self._make_txn("t3", date(2026, 6, 11), Decimal("238.45"), Decimal("5841.84"))

        ArcopayService._update_account_balance(self.account)

        self.account.refresh_from_db()
        self.assertEqual(self.account.last_known_balance, Money("5841.84", "EUR"))
