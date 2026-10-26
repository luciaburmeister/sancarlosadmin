from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from djmoney.money import Money

from invoices.models import (
    Invoice,
    InvoiceDocumentType,
    InvoicePaymentStatus,
    SalesInvoice,
    SalesInvoiceStatus,
)
from invoices.tasks import send_pending_invoices_digest
from invoices.tests.digest_fixtures import (
    make_company,
    make_purchase_invoice,
    make_sales_invoice,
)
from invoices.services.pending_invoices_digest_service import (
    PendingInvoicesDigestService,
)
from materials.models import Provider
from payments.models import Payment, PaymentAllocation, PaymentDirection
from sales.models import Client


@patch("core.services.email_service.EmailService.send_email", return_value=True)
class SendPendingInvoicesDigestTests(TestCase):
    def _make_company(self, name, emails):
        return make_company(name=name, invoice_digest_emails=emails)

    def _make_invoice(self, company, number, total="100.00", due_date=None, **kwargs):
        return make_purchase_invoice(
            company=company, number=number, total=total, due_date=due_date, **kwargs,
        )

    def _make_sales_invoice(self, company, number, total="200.00", due_date=None, **kwargs):
        return make_sales_invoice(
            company=company, number=number, total=total, due_date=due_date, **kwargs,
        )

    def _allocate_payment(self, invoice, amount, direction=PaymentDirection.OUTBOUND):
        payment = Payment.objects.create(
            company=invoice.company,
            payment_date=timezone.localdate(),
            amount=Money(amount, "EUR"),
            direction=direction,
            method="transfer",
        )
        return PaymentAllocation.objects.create(
            payment=payment,
            amount_applied=Money(amount, "EUR"),
            content_type=ContentType.objects.get_for_model(type(invoice)),
            object_id=invoice.pk,
        )

    # ── Recipient grouping (mirrors banking bank-summary behaviour) ──

    def test_shared_recipient_gets_one_consolidated_email(self, mock_send):
        alpha = self._make_company("Alpha", ["shared@example.com"])
        beta = self._make_company("Beta", ["shared@example.com"])
        self._make_invoice(alpha, "F-A1")
        self._make_invoice(beta, "F-B1")

        result = send_pending_invoices_digest()

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
        self._make_invoice(alpha, "F-A1")

        result = send_pending_invoices_digest()

        self.assertEqual(result["emails_sent"], 1)
        kwargs = mock_send.call_args.kwargs
        self.assertCountEqual(kwargs["to"], ["a@example.com", "b@example.com"])
        self.assertEqual(kwargs["company"], alpha)

    def test_recipient_email_deduplicated_case_insensitively(self, mock_send):
        self._make_company("Alpha", ["Shared@Example.com"])
        self._make_company("Beta", ["shared@example.com "])

        result = send_pending_invoices_digest()

        self.assertEqual(result["emails_sent"], 1)
        self.assertEqual(len(mock_send.call_args.kwargs["to"]), 1)

    def test_company_without_recipients_is_not_emailed(self, mock_send):
        company = make_company(name="Silent")
        self._make_invoice(company, "F-S1")

        result = send_pending_invoices_digest()

        self.assertEqual(result["emails_sent"], 0)
        mock_send.assert_not_called()

    # ── Content: totals, rows and pre-filtered links ─────────────────

    def test_totals_counterparties_and_prefiltered_links_in_body(self, mock_send):
        company = self._make_company("Alpha", ["boss@example.com"])
        yesterday = timezone.localdate() - timedelta(days=1)
        tomorrow = timezone.localdate() + timedelta(days=1)
        invoice = self._make_invoice(
            company, "F-100", total="100.00", due_date=yesterday,
            payment_status=InvoicePaymentStatus.PARTIAL,
        )
        self._allocate_payment(invoice, "40.00")
        self._make_sales_invoice(company, "V-250", total="250.00", due_date=tomorrow)

        send_pending_invoices_digest()

        body = mock_send.call_args.kwargs["body_html"]
        self.assertIn("Proveedor F-100", body)
        self.assertIn("Cliente V-250", body)
        # A partially paid invoice shows what is still owed, not its total
        self.assertIn("60,00", body)
        self.assertIn("250,00", body)
        # Net position: 250 - 60 = 190
        self.assertIn("190,00", body)
        # Pre-filtered admin links for both changelists
        self.assertIn(reverse("admin:invoices_invoice_changelist"), body)
        self.assertIn(reverse("admin:invoices_salesinvoice_changelist"), body)
        self.assertIn(f"company__id__exact={company.pk}", body)
        self.assertIn("payment_status__in=unpaid,partial", body)
        self.assertIn("status__exact=final", body)
        # Overdue purchase invoice is flagged
        self.assertIn("vencida", body)

    def test_credit_note_nets_payables_total(self, mock_send):
        company = self._make_company("Alpha", ["boss@example.com"])
        self._make_invoice(company, "F-100", total="100.00")
        self._make_invoice(
            company, "R-020", total="-20.00",
            document_type=InvoiceDocumentType.CREDIT_NOTE,
        )

        send_pending_invoices_digest()

        body = mock_send.call_args.kwargs["body_html"]
        self.assertIn("80,00", body)

    def test_paid_and_proforma_documents_are_excluded(self, mock_send):
        company = self._make_company("Alpha", ["boss@example.com"])
        self._make_invoice(
            company, "F-PAID", payment_status=InvoicePaymentStatus.PAID
        )
        self._make_sales_invoice(
            company, "V-PROF", status=SalesInvoiceStatus.PROFORMA
        )

        result = send_pending_invoices_digest()

        # The company still gets its (empty) daily report.
        self.assertEqual(result["emails_sent"], 1)
        body = mock_send.call_args.kwargs["body_html"]
        self.assertNotIn("F-PAID", body)
        self.assertNotIn("V-PROF", body)
        self.assertIn("Sin facturas pendientes de pago", body)
        self.assertIn("Sin facturas pendientes de cobro", body)


class DigestPrefilteredChangelistTests(TestCase):
    """The links embedded in the digest must be valid admin changelist URLs."""

    def setUp(self):
        User = get_user_model()
        self.client.force_login(
            User.objects.create_superuser("digest-admin", "admin@test.com", "pass")
        )
        self.company = make_company(name="Alpha")
        provider = Provider.objects.create(company=self.company, name="Proveedor")
        self.unpaid = make_purchase_invoice(
            company=self.company, provider=provider, number="AP-OPEN-1",
        )
        self.paid = make_purchase_invoice(
            company=self.company, provider=provider, number="AP-SETTLED-1",
            payment_status=InvoicePaymentStatus.PAID,
        )
        client = Client.objects.create(company=self.company, name="Cliente")
        self.final_sale = make_sales_invoice(
            company=self.company, client=client, number="SALE-FINAL-1", total="50.00",
        )
        self.proforma_sale = make_sales_invoice(
            company=self.company, client=client, number="SALE-PROF-1", total="50.00",
            status=SalesInvoiceStatus.PROFORMA,
        )

    def test_payables_link_filters_to_pending_invoices(self):
        response = self.client.get(
            reverse("admin:invoices_invoice_changelist"),
            {
                "company__id__exact": self.company.pk,
                "payment_status__in": "unpaid,partial",
            },
        )
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertIn("AP-OPEN-1", content)
        self.assertNotIn("AP-SETTLED-1", content)

    def test_receivables_link_filters_to_pending_final_invoices(self):
        response = self.client.get(
            reverse("admin:invoices_salesinvoice_changelist"),
            {
                "company__id__exact": self.company.pk,
                "status__exact": "final",
                "payment_status__in": "unpaid,partial",
            },
        )
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertIn("SALE-FINAL-1", content)
        self.assertNotIn("SALE-PROF-1", content)


class PendingInvoicesDigestServiceTests(TestCase):
    """The service returns raw data (Decimals, dates) and batches queries."""

    def test_returns_raw_decimals_dates_and_overdue_flags(self):
        company = make_company(name="Alpha")
        provider = Provider.objects.create(company=company, name="ACME")
        yesterday = timezone.localdate() - timedelta(days=1)
        invoice = make_purchase_invoice(
            company=company, provider=provider, number="F-100", due_date=yesterday,
            payment_status=InvoicePaymentStatus.PARTIAL,
        )
        payment = Payment.objects.create(
            company=company, payment_date=timezone.localdate(),
            amount=Money("40.00", "EUR"),
            direction=PaymentDirection.OUTBOUND, method="transfer",
        )
        PaymentAllocation.objects.create(
            payment=payment, amount_applied=Money("40.00", "EUR"),
            content_type=ContentType.objects.get_for_model(Invoice),
            object_id=invoice.pk,
        )

        digest = PendingInvoicesDigestService.get_company_digests([company])[company.pk]

        row = digest.payables.rows[0]
        self.assertEqual(row.remaining, Decimal("60.00"))
        self.assertEqual(row.due_date, yesterday)
        self.assertEqual(row.currency, "EUR")
        self.assertTrue(row.overdue)
        self.assertEqual(digest.payables.totals, {"EUR": Decimal("60.00")})
        self.assertEqual(digest.payables.overdue_count, 1)
        self.assertEqual(digest.receivables.count, 0)

    def test_invoice_without_due_date_counts_as_overdue(self):
        company = make_company(name="Beta")
        provider = Provider.objects.create(company=company, name="ACME")
        make_purchase_invoice(
            company=company, provider=provider, number="F-NODATE", total="75.00",
            due_date=None,
        )

        digest = PendingInvoicesDigestService.get_company_digests([company])[company.pk]

        self.assertTrue(digest.payables.rows[0].overdue)
        self.assertEqual(digest.payables.overdue_count, 1)
        self.assertEqual(digest.payables.overdue_totals, {"EUR": Decimal("75.00")})

    def test_batches_to_two_queries_regardless_of_company_count(self):
        companies = []
        for i in range(3):
            company = make_company(name=f"C{i}")
            provider = Provider.objects.create(company=company, name=f"P{i}")
            make_purchase_invoice(
                company=company, provider=provider, number=f"F-{i}", total="10.00",
            )
            client = Client.objects.create(company=company, name=f"K{i}")
            make_sales_invoice(
                company=company, client=client, number=f"V-{i}", total="20.00",
            )
            companies.append(company)

        # Warm the ContentType cache so it doesn't count as a query.
        ContentType.objects.get_for_model(Invoice)
        ContentType.objects.get_for_model(SalesInvoice)

        with self.assertNumQueries(2):
            digests = PendingInvoicesDigestService.get_company_digests(companies)

        self.assertEqual(len(digests), 3)
        self.assertEqual(
            digests[companies[0].pk].payables.totals, {"EUR": Decimal("10.00")}
        )
        self.assertEqual(
            digests[companies[2].pk].receivables.totals, {"EUR": Decimal("20.00")}
        )

    def test_merge_totals_consolidates_currencies(self):
        merged = PendingInvoicesDigestService.merge_totals([
            {"EUR": Decimal("10.00"), "USD": Decimal("5.00")},
            {"EUR": Decimal("2.50")},
        ])
        self.assertEqual(merged, {"EUR": Decimal("12.50"), "USD": Decimal("5.00")})

