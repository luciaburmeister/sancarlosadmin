"""
Tests for PSD2 consent status tracking on BankAccount.

``ArcopayService.check_consent_status`` queries /consent/info/ and persists
the result (status, raw message, expiry, checked-at). ``sync_account`` runs
the check first and skips the sync when the consent is invalid. Status
changes are recorded in the account's simple-history audit trail; mere
re-checks only bump ``consent_checked_at`` without creating history rows.
"""
from datetime import date, timedelta
from unittest.mock import patch

from django.test import TestCase

from banking.services.arcopay_import import ArcopayService
from company.models import BankAccount, Company, ConsentStatus


def _make_account(company, **overrides):
    defaults = dict(
        company=company,
        bank_name="Test Bank",
        iban="ES9121000418450200051332",
        nuek_token="tok-123",
        nuek_consent_id="consent-123",
        nuek_product_id="ES9121000418450200051332",
    )
    defaults.update(overrides)
    return BankAccount.objects.create(**defaults)


class CheckConsentStatusTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Test Co")
        self.account = _make_account(self.company)

    def _check(self, info):
        with patch.object(ArcopayService, "get_consent_info", return_value=info):
            return ArcopayService.check_consent_status(self.account)

    def test_valid_consent(self):
        until = date.today() + timedelta(days=30)
        status = self._check({
            "message": "Valid",
            "validUntil": until.strftime("%d-%m-%Y"),
        })
        self.account.refresh_from_db()
        self.assertEqual(status, ConsentStatus.VALID)
        self.assertEqual(self.account.consent_status, ConsentStatus.VALID)
        self.assertEqual(self.account.consent_status_message, "Valid")
        self.assertEqual(self.account.consent_valid_until, until)
        self.assertIsNotNone(self.account.consent_checked_at)

    def test_rejected_consent(self):
        status = self._check({"message": "Rejected", "validUntil": "01-10-2026"})
        self.account.refresh_from_db()
        self.assertEqual(status, ConsentStatus.INVALID)
        self.assertEqual(self.account.consent_status_message, "Rejected")

    def test_valid_message_but_expired_date_is_invalid(self):
        past = date.today() - timedelta(days=1)
        status = self._check({
            "message": "Valid",
            "validUntil": past.strftime("%d-%m-%Y"),
        })
        self.assertEqual(status, ConsentStatus.INVALID)

    def test_dead_consent_error_code_is_invalid(self):
        """Arcopay may answer /consent/info/ with a C001 error instead of a
        status once the consent is dead — that must map to INVALID."""
        from banking.services.arcopay_import import ArcopayAPIError

        with patch.object(
            ArcopayService, "get_consent_info",
            side_effect=ArcopayAPIError(
                "The consent is not valid anymore, please request a new one.", "C001",
            ),
        ):
            status = ArcopayService.check_consent_status(self.account)
        self.account.refresh_from_db()
        self.assertEqual(status, ConsentStatus.INVALID)
        self.assertIn("not valid anymore", self.account.consent_status_message)

    def test_unrecognised_message_stays_unknown(self):
        status = self._check({"message": "SomethingNew"})
        self.assertEqual(status, ConsentStatus.UNKNOWN)

    def test_no_token_is_unknown_without_api_call(self):
        self.account.nuek_token = ""
        self.account.save()
        with patch.object(ArcopayService, "get_consent_info") as mock_info:
            status = ArcopayService.check_consent_status(self.account)
        mock_info.assert_not_called()
        self.assertEqual(status, ConsentStatus.UNKNOWN)

    def test_status_change_creates_history_row(self):
        baseline = self.account.history.count()
        self._check({"message": "Rejected"})
        self.assertEqual(self.account.history.count(), baseline + 1)
        self.assertEqual(
            self.account.history.first().consent_status, ConsentStatus.INVALID,
        )

    def test_recheck_without_change_creates_no_history_row(self):
        self._check({"message": "Rejected"})
        baseline = self.account.history.count()
        first_checked_at = BankAccount.objects.get(pk=self.account.pk).consent_checked_at

        self._check({"message": "Rejected"})

        self.assertEqual(self.account.history.count(), baseline)
        self.account.refresh_from_db()
        self.assertGreaterEqual(self.account.consent_checked_at, first_checked_at)


class SyncSkipsInvalidConsentTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Test Co")
        self.account = _make_account(self.company)

    def test_sync_skipped_when_consent_invalid(self):
        with patch.object(
            ArcopayService, "get_consent_info", return_value={"message": "Rejected"},
        ), patch.object(ArcopayService, "fetch_transactions") as mock_fetch:
            imported = ArcopayService.sync_account(self.account)
        mock_fetch.assert_not_called()
        self.assertEqual(imported, 0)

    def test_sync_proceeds_when_consent_valid(self):
        with patch.object(
            ArcopayService, "get_consent_info", return_value={"message": "Valid"},
        ), patch.object(
            ArcopayService, "fetch_transactions",
            return_value=[{
                "product": self.account.nuek_product_id,
                "currency": "EUR",
                "transactions": [],
            }],
        ) as mock_fetch:
            ArcopayService.sync_account(self.account)
        mock_fetch.assert_called()

    def test_sync_proceeds_when_consent_check_errors(self):
        """A transient consent lookup failure must not block the sync."""
        import requests

        with patch.object(
            ArcopayService, "get_consent_info",
            side_effect=requests.ConnectionError("boom"),
        ), patch.object(
            ArcopayService, "fetch_transactions",
            return_value=[{
                "product": self.account.nuek_product_id,
                "currency": "EUR",
                "transactions": [],
            }],
        ) as mock_fetch:
            ArcopayService.sync_account(self.account)
        mock_fetch.assert_called()
