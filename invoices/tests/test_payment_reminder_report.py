from datetime import timedelta
from unittest.mock import patch

from celery.exceptions import Retry
from django.test import TestCase
from django.utils import timezone
from djmoney.money import Money

from company.models import Company
from invoices.models import (
    PaymentReminderLog,
    PaymentReminderLogStatus,
    PaymentReminderRule,
    PaymentReminderSettings,
    PaymentReminderTriggeredBy,
    PaymentReminderType,
    SalesInvoice,
    SalesInvoiceStatus,
)
from invoices.tasks import process_payment_reminders, send_payment_reminder_report
from sales.models import Client


@patch("core.services.email_service.EmailService.send_email", return_value=True)
class PaymentReminderReportTests(TestCase):
    def setUp(self):
        self.today = timezone.localdate()

    # ── Helpers ───────────────────────────────────────────────────────

    def _make_company(self, name="Alpha", email=None):
        return Company.objects.create(name=name, email=email)

    def _make_settings(self, company, report_emails=None, **kwargs):
        return PaymentReminderSettings.objects.create(
            company=company, report_emails=report_emails or [], **kwargs
        )

    def _make_invoice(self, company, number, client_name="Cliente", client_email="c@example.com", due_date=None):
        client = Client.objects.create(company=company, name=client_name, email=client_email)
        return SalesInvoice.objects.create(
            company=company,
            client=client,
            invoice_number=number,
            total_amount=Money("100.00", "EUR"),
            due_date=due_date or self.today,
            status=SalesInvoiceStatus.FINAL,
        )

    def _make_log(self, invoice, status, error_message="", **kwargs):
        kwargs.setdefault("reminder_type", PaymentReminderType.OVERDUE)
        kwargs.setdefault("scheduled_for", self.today)
        if status == PaymentReminderLogStatus.SENT:
            kwargs.setdefault("sent_at", timezone.now())
        return PaymentReminderLog.objects.create(
            company=invoice.company,
            invoice=invoice,
            status=status,
            error_message=error_message,
            **kwargs,
        )

    def _run_report(self, company):
        return send_payment_reminder_report.delay(company.pk, self.today.isoformat()).get()

    # ── Recipients ────────────────────────────────────────────────────

    def test_report_goes_to_configured_emails(self, mock_send):
        company = self._make_company(email="info@alpha.com")
        self._make_settings(company, report_emails=["cobros@alpha.com", "admin@alpha.com"])
        self._make_log(self._make_invoice(company, "F-1"), PaymentReminderLogStatus.SENT)

        result = self._run_report(company)

        self.assertEqual(result["status"], "ok")
        mock_send.assert_called_once()
        kwargs = mock_send.call_args.kwargs
        self.assertEqual(kwargs["to"], ["cobros@alpha.com", "admin@alpha.com"])
        self.assertEqual(kwargs["company"], company)
        self.assertEqual(kwargs["email_document"], "payment_reminder_report")

    def test_report_falls_back_to_company_email(self, mock_send):
        company = self._make_company(email="info@alpha.com")
        self._make_settings(company)
        self._make_log(self._make_invoice(company, "F-1"), PaymentReminderLogStatus.SENT)

        self._run_report(company)

        self.assertEqual(mock_send.call_args.kwargs["to"], ["info@alpha.com"])

    def test_report_skipped_without_any_recipient(self, mock_send):
        company = self._make_company(email=None)
        self._make_settings(company)
        self._make_log(self._make_invoice(company, "F-1"), PaymentReminderLogStatus.SENT)

        result = self._run_report(company)

        self.assertEqual(result["status"], "skipped")
        mock_send.assert_not_called()

    def test_no_report_on_a_day_without_reminders(self, mock_send):
        company = self._make_company(email="info@alpha.com")
        self._make_settings(company)
        # A reminder from another day must not trigger today's report.
        self._make_log(
            self._make_invoice(company, "F-old"),
            PaymentReminderLogStatus.SENT,
            scheduled_for=self.today - timedelta(days=1),
        )

        result = self._run_report(company)

        self.assertEqual(result["status"], "skipped")
        mock_send.assert_not_called()

    # ── Content ───────────────────────────────────────────────────────

    def test_report_lists_each_reminder_with_its_outcome(self, mock_send):
        company = self._make_company(email="info@alpha.com")
        self._make_settings(company)
        sent = self._make_invoice(company, "F-SENT", "Cliente Uno", "uno@example.com")
        failed = self._make_invoice(company, "F-FAIL", "Cliente Dos", "dos@example.com")
        cancelled = self._make_invoice(company, "F-CANC", "Cliente Tres", "tres@example.com")
        self._make_log(sent, PaymentReminderLogStatus.SENT)
        self._make_log(failed, PaymentReminderLogStatus.FAILED, error_message="SMTP connection refused")
        self._make_log(cancelled, PaymentReminderLogStatus.CANCELLED)

        result = self._run_report(company)

        self.assertEqual(result["reminders"], 3)
        kwargs = mock_send.call_args.kwargs
        body = kwargs["body_html"]
        for fragment in (
            "Cliente Uno", "uno@example.com", "F-SENT",
            "Cliente Dos", "dos@example.com", "F-FAIL", "SMTP connection refused",
            "Cliente Tres", "tres@example.com", "F-CANC",
        ):
            self.assertIn(fragment, body)
        self.assertIn("status-sent", body)
        self.assertIn("status-failed", body)
        self.assertIn("status-cancelled", body)
        # Problems are listed before successes.
        self.assertLess(body.index("F-FAIL"), body.index("F-SENT"))
        self.assertLess(body.index("F-SENT"), body.index("F-CANC"))

        self.assertIn("Alpha", kwargs["subject"])
        self.assertIn("1 enviado", kwargs["subject"])
        self.assertIn("1 fallido", kwargs["subject"])
        self.assertIn("1 cancelado", kwargs["subject"])

    def test_report_links_to_the_logs_of_that_company_and_day(self, mock_send):
        company = self._make_company(email="info@alpha.com")
        self._make_settings(company)
        self._make_log(self._make_invoice(company, "F-1"), PaymentReminderLogStatus.SENT)

        self._run_report(company)

        body = mock_send.call_args.kwargs["body_html"]
        day = self.today.isoformat()
        self.assertIn(f"company__id__exact={company.pk}", body)
        self.assertIn(f"scheduled_for_from={day}", body)
        self.assertIn(f"scheduled_for_to={day}", body)

    def test_manual_reminders_of_the_day_are_included(self, mock_send):
        company = self._make_company(email="info@alpha.com")
        self._make_settings(company)
        self._make_log(
            self._make_invoice(company, "F-MAN"),
            PaymentReminderLogStatus.SENT,
            triggered_by=PaymentReminderTriggeredBy.MANUAL,
        )

        self._run_report(company)

        self.assertIn("F-MAN", mock_send.call_args.kwargs["body_html"])

    def test_waits_while_reminders_are_still_pending(self, mock_send):
        company = self._make_company(email="info@alpha.com")
        self._make_settings(company)
        self._make_log(self._make_invoice(company, "F-PEND"), PaymentReminderLogStatus.PENDING)

        with self.assertRaises(Retry):
            self._run_report(company)

        mock_send.assert_not_called()

    def test_still_pending_reminders_are_reported_on_the_last_attempt(self, mock_send):
        company = self._make_company(email="info@alpha.com")
        self._make_settings(company)
        self._make_log(self._make_invoice(company, "F-PEND"), PaymentReminderLogStatus.PENDING)

        result = send_payment_reminder_report.apply(
            args=[company.pk, self.today.isoformat()],
            retries=send_payment_reminder_report.max_retries,
        ).get()

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["pending"], 1)
        kwargs = mock_send.call_args.kwargs
        self.assertIn("1 pendiente", kwargs["subject"])
        self.assertIn("status-pending", kwargs["body_html"])

    # ── Orchestration from process_payment_reminders ─────────────────

    def test_process_payment_reminders_sends_report_after_reminders(self, mock_send):
        company = self._make_company(email="info@alpha.com")
        now = timezone.localtime().replace(second=0, microsecond=0)
        reminder_settings = self._make_settings(
            company, report_emails=["cobros@alpha.com"], send_time=now.time()
        )
        PaymentReminderRule.objects.create(
            company=company, settings=reminder_settings, days_before_due=3
        )
        invoice = self._make_invoice(
            company, "F-DUE", due_date=self.today + timedelta(days=3)
        )

        result = process_payment_reminders()

        self.assertEqual(result["reminders_queued"], 1)
        documents = [c.kwargs["email_document"] for c in mock_send.call_args_list]
        self.assertEqual(documents, ["payment_reminder", "payment_reminder_report"])

        reminder_call, report_call = mock_send.call_args_list
        self.assertEqual(reminder_call.kwargs["to"], [invoice.client.email])
        self.assertEqual(report_call.kwargs["to"], ["cobros@alpha.com"])
        self.assertIn("F-DUE", report_call.kwargs["body_html"])
        self.assertIn(invoice.client.email, report_call.kwargs["body_html"])
        self.assertIn("1 enviado", report_call.kwargs["subject"])
        log = PaymentReminderLog.objects.get(invoice=invoice)
        self.assertEqual(log.status, PaymentReminderLogStatus.SENT)
