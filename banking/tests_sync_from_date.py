"""
Tests for the configurable bank account sync start date.

``BankAccount.sync_from_date`` controls how far back ``sync_account``
fetches transactions from Arcopay. When unset, the default rolling
window (``days_back``, 90 days) applies. The date is one-shot: it is
cleared after a successful sync, and kept if the sync fails.
"""
from datetime import date, timedelta
from unittest.mock import patch

from django.test import TestCase

from banking.services.arcopay_import import ArcopayAPIError, ArcopayService
from company.models import BankAccount, Company, ConsentStatus


class SyncFromDateTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Test Co")
        self.account = BankAccount.objects.create(
            company=self.company,
            bank_name="Test Bank",
            iban="ES9121000418450200051332",
            nuek_token="tok-123",
            nuek_consent_id="consent-123",
            nuek_product_id="ES9121000418450200051332",
        )
        consent_patch = patch.object(
            ArcopayService, "check_consent_status", return_value=ConsentStatus.VALID,
        )
        consent_patch.start()
        self.addCleanup(consent_patch.stop)

    def _sync_and_capture_start_dates(self, **sync_kwargs):
        """Run sync_account with the API mocked; return the startDates used."""
        calls = []

        def fake_fetch(token, start_date, products="GLOBAL"):
            calls.append({"start_date": start_date, "products": products})
            return [{
                "product": self.account.nuek_product_id,
                "currency": "EUR",
                "transactions": [],
            }]

        with patch.object(ArcopayService, "fetch_transactions", side_effect=fake_fetch):
            ArcopayService.sync_account(self.account, **sync_kwargs)
        return calls

    def test_sync_defaults_to_90_days_without_sync_from_date(self):
        calls = self._sync_and_capture_start_dates()
        self.assertTrue(calls)
        for call in calls:
            self.assertEqual(call["start_date"], date.today() - timedelta(days=90))

    def test_days_back_override_used_when_no_sync_from_date(self):
        calls = self._sync_and_capture_start_dates(days_back=30)
        self.assertTrue(calls)
        for call in calls:
            self.assertEqual(call["start_date"], date.today() - timedelta(days=30))

    def test_sync_from_date_wins_over_days_back(self):
        self.account.sync_from_date = date(2026, 1, 1)
        self.account.save()

        calls = self._sync_and_capture_start_dates(days_back=30)
        self.assertTrue(calls)
        for call in calls:
            self.assertEqual(call["start_date"], date(2026, 1, 1))

    def test_sync_from_date_cleared_after_successful_sync(self):
        """The backfill date is one-shot: gone once the sync succeeds."""
        self.account.sync_from_date = date(2026, 1, 1)
        self.account.save()

        self._sync_and_capture_start_dates()
        self.account.refresh_from_db()
        self.assertIsNone(self.account.sync_from_date)

    def test_sync_from_date_kept_when_fetch_fails(self):
        """A failed sync must not consume the backfill date."""
        self.account.sync_from_date = date(2026, 1, 1)
        self.account.save()

        # T000 (rate limit) short-circuits the token-refresh fallback
        with patch.object(
            ArcopayService,
            "fetch_transactions",
            side_effect=ArcopayAPIError("rate limited", "T000"),
        ):
            ArcopayService.sync_account(self.account)

        self.account.refresh_from_db()
        self.assertEqual(self.account.sync_from_date, date(2026, 1, 1))

    def test_sync_without_token_calls_nothing_and_keeps_sync_from_date(self):
        self.account.nuek_token = ""
        self.account.sync_from_date = date(2026, 1, 1)
        self.account.save()

        calls = self._sync_and_capture_start_dates()
        self.assertEqual(calls, [])
        self.account.refresh_from_db()
        self.assertEqual(self.account.sync_from_date, date(2026, 1, 1))


class MultiProductSyncFromDateTests(TestCase):
    """One consent covering several products: each product's account uses and
    clears its own sync_from_date."""

    def setUp(self):
        self.company = Company.objects.create(name="Test Co")
        # Triggering account: knows both products, backfill from Jan 1st
        self.account_a = BankAccount.objects.create(
            company=self.company,
            bank_name="Bank A",
            iban="ES9121000418450200051332",
            nuek_token="tok-123",
            nuek_consent_id="consent-123",
            nuek_product_id="P1,P2",
            sync_from_date=date(2026, 1, 1),
        )
        # Second product's own account, backfill from Feb 1st
        self.account_b = BankAccount.objects.create(
            company=self.company,
            bank_name="Bank B",
            iban="ES7921000813610123456789",
            nuek_token="tok-123",
            nuek_consent_id="consent-123",
            nuek_product_id="P2",
            sync_from_date=date(2026, 2, 1),
        )
        consent_patch = patch.object(
            ArcopayService, "check_consent_status", return_value=ConsentStatus.VALID,
        )
        consent_patch.start()
        self.addCleanup(consent_patch.stop)

    def _run_sync(self, fail_products=()):
        calls = {}

        def fake_fetch(token, start_date, products="GLOBAL"):
            if products in fail_products:
                raise ArcopayAPIError("rate limited", "T000")
            calls[products] = start_date
            return [{"product": products, "currency": "EUR", "transactions": []}]

        with patch.object(ArcopayService, "fetch_transactions", side_effect=fake_fetch):
            ArcopayService.sync_account(self.account_a)
        return calls

    def test_each_product_uses_its_own_accounts_date(self):
        calls = self._run_sync()
        # P1 has no dedicated account → falls back to the triggering account
        self.assertEqual(calls["P1"], date(2026, 1, 1))
        self.assertEqual(calls["P2"], date(2026, 2, 1))

    def test_each_product_clears_its_own_accounts_date(self):
        self._run_sync()
        self.account_a.refresh_from_db()
        self.account_b.refresh_from_db()
        self.assertIsNone(self.account_a.sync_from_date)
        self.assertIsNone(self.account_b.sync_from_date)

    def test_failed_product_keeps_its_date_others_clear(self):
        self._run_sync(fail_products=("P2",))
        self.account_a.refresh_from_db()
        self.account_b.refresh_from_db()
        self.assertIsNone(self.account_a.sync_from_date)      # P1 synced fine
        self.assertEqual(self.account_b.sync_from_date, date(2026, 2, 1))
