from unittest.mock import patch

from django.test import TestCase
from djmoney.money import Money

from banking.tasks import send_bank_accounts_summary
from company.models import BankAccount, Company


@patch("core.services.email_service.EmailService.send_email", return_value=True)
class SendBankAccountsSummaryTests(TestCase):
    def _make_company(self, name, emails):
        company = Company.objects.create(name=name, bank_summary_emails=emails)
        BankAccount.objects.create(
            company=company,
            bank_name=f"Banco {name}",
            iban="ES9121000418450200051332",
            last_known_balance=Money("1000.00", "EUR"),
        )
        return company

    def test_shared_recipient_gets_one_consolidated_email(self, mock_send):
        self._make_company("Alpha", ["shared@example.com"])
        self._make_company("Beta", ["shared@example.com"])

        result = send_bank_accounts_summary()

        self.assertEqual(result["emails_sent"], 1)
        mock_send.assert_called_once()
        kwargs = mock_send.call_args.kwargs
        self.assertEqual(kwargs["to"], ["shared@example.com"])
        self.assertIn("Alpha", kwargs["subject"])
        self.assertIn("Beta", kwargs["subject"])
        self.assertIn("Alpha", kwargs["body_html"])
        self.assertIn("Beta", kwargs["body_html"])
        # Consolidated email is not attributed to a single company.
        self.assertIsNone(kwargs["company"])

    def test_recipients_of_same_single_company_are_batched(self, mock_send):
        alpha = self._make_company("Alpha", ["a@example.com", "b@example.com"])

        result = send_bank_accounts_summary()

        self.assertEqual(result["emails_sent"], 1)
        kwargs = mock_send.call_args.kwargs
        self.assertCountEqual(kwargs["to"], ["a@example.com", "b@example.com"])
        self.assertEqual(kwargs["subject"], "Resumen de cuentas bancarias: Alpha")
        self.assertEqual(kwargs["company"], alpha)

    def test_mixed_recipients_split_into_correct_groups(self, mock_send):
        self._make_company("Alpha", ["shared@example.com", "alpha-only@example.com"])
        self._make_company("Beta", ["shared@example.com"])

        result = send_bank_accounts_summary()

        self.assertEqual(result["emails_sent"], 2)
        sent = {tuple(c.kwargs["to"]): c.kwargs for c in mock_send.call_args_list}
        self.assertIn(("shared@example.com",), sent)
        self.assertIn(("alpha-only@example.com",), sent)
        self.assertIn("Beta", sent[("shared@example.com",)]["body_html"])
        self.assertNotIn("Beta", sent[("alpha-only@example.com",)]["body_html"])

    def test_recipient_email_deduplicated_case_insensitively(self, mock_send):
        self._make_company("Alpha", ["Shared@Example.com"])
        self._make_company("Beta", ["shared@example.com "])

        result = send_bank_accounts_summary()

        self.assertEqual(result["emails_sent"], 1)
        self.assertEqual(len(mock_send.call_args.kwargs["to"]), 1)

    def test_company_without_accounts_is_skipped(self, mock_send):
        Company.objects.create(name="Empty", bank_summary_emails=["x@example.com"])

        result = send_bank_accounts_summary()

        self.assertEqual(result["emails_sent"], 0)
        mock_send.assert_not_called()
