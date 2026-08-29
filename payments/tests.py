"""Tests for the shared "Register Payment" admin screen.

The Invoice, SalesInvoice and ExpenseClaim screens are all thin subclasses of
`payments.views.RegisterPaymentView`, so these cover the two invoice documents
here and the claim-specific behaviour lives in `apps.expenses.tests`.
"""
from datetime import date, timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import translation
from djmoney.money import Money

from company.models import Company
from invoices.models import (
    Invoice,
    InvoicePaymentStatus,
    SalesInvoice,
    SalesInvoiceStatus,
)
from materials.models import Provider
from payments.models import Payment, PaymentAllocation, PaymentDirection, PaymentMethod
from sales.models import Client

User = get_user_model()


class RegisterPaymentViewTest(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Global Corp")
        self.user = User.objects.create_superuser(username="finance", password="pw", email="fin@example.com")
        self.client.force_login(self.user)
        self.today = date(2026, 7, 3)
        self.provider = Provider.objects.create(company=self.company, name="Perfiles SA")

    def _post(self, url_name, pk, amount):
        # LocaleMiddleware activates the URL prefix's language and never
        # restores it, so the request has to run inside the override.
        with translation.override("en"):
            return self.client.post(reverse(url_name, args=[pk]), {
                "amount": amount,
                "currency": "EUR",
                "payment_date": "2026-07-03",
                "method": PaymentMethod.TRANSFER,
                "reference": "REF-1",
            })

    def _get(self, url_name, pk):
        with translation.override("en"):
            return self.client.get(reverse(url_name, args=[pk]))

    # ── Purchase invoice: money goes out ─────────────────────────────────────

    def _purchase_invoice(self):
        return Invoice.objects.create(
            company=self.company,
            provider=self.provider,
            invoice_number="F-001",
            date_issued=self.today,
            due_date=self.today + timedelta(days=30),
            payment_status=InvoicePaymentStatus.UNPAID,
            total_amount=Money("1210.00", "EUR"),
        )

    def test_purchase_invoice_page_renders(self):
        invoice = self._purchase_invoice()
        response = self._get("admin:invoices_invoice_register_payment", invoice.pk)
        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        self.assertIn("Register Payment", body)
        self.assertIn("1,210.00", body)

    def test_paying_a_purchase_invoice_creates_an_outbound_payment(self):
        invoice = self._purchase_invoice()
        response = self._post("admin:invoices_invoice_register_payment", invoice.pk, "1210.00")
        self.assertEqual(response.status_code, 302)

        payment = Payment.objects.get()
        self.assertEqual(payment.direction, PaymentDirection.OUTBOUND)
        self.assertEqual(payment.amount, Money(Decimal("1210.00"), "EUR"))
        self.assertEqual(PaymentAllocation.objects.get().related_object, invoice)

        invoice.refresh_from_db()
        self.assertEqual(invoice.payment_status, InvoicePaymentStatus.PAID)

    def test_purchase_invoices_still_allow_over_allocation(self):
        # No cap is declared for invoices, unlike expense claims.
        invoice = self._purchase_invoice()
        response = self._post("admin:invoices_invoice_register_payment", invoice.pk, "2000.00")
        self.assertEqual(response.status_code, 302)
        self.assertTrue(Payment.objects.exists())

    # ── Sales invoice: money comes in ────────────────────────────────────────

    def _sales_invoice(self):
        return SalesInvoice.objects.create(
            company=self.company,
            client=Client.objects.create(company=self.company, name="Cliente SA"),
            invoice_number="V-001",
            status=SalesInvoiceStatus.FINAL,
            date_issued=self.today,
            due_date=self.today + timedelta(days=30),
        )

    def test_paying_a_sales_invoice_creates_an_inbound_payment(self):
        invoice = self._sales_invoice()
        response = self._post("admin:invoices_salesinvoice_register_payment", invoice.pk, "50.00")
        self.assertEqual(response.status_code, 302)

        payment = Payment.objects.get()
        self.assertEqual(payment.direction, PaymentDirection.INBOUND)
        self.assertEqual(PaymentAllocation.objects.get().related_object, invoice)
