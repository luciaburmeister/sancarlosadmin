"""Tests for the shared "Register Payment" admin screen.

The Invoice, SalesInvoice and ExpenseClaim screens are all thin subclasses of
`payments.views.RegisterPaymentView`, so these cover the two invoice documents
here and the claim-specific behaviour lives in `apps.expenses.tests`.
"""
from datetime import date, timedelta
from decimal import Decimal

from django.test import TestCase
from djmoney.money import Money

from invoices.models import (
    Invoice,
    InvoicePaymentStatus,
    SalesInvoice,
    SalesInvoiceStatus,
)
from materials.models import Provider
from payments.models import Payment, PaymentAllocation, PaymentDirection, PaymentMethod
from payments.testing import CompanyAdminTestMixin, payment_form
from sales.models import Client


class RegisterPaymentViewTest(CompanyAdminTestMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.user = self.login_superuser()
        self.today = date(2026, 7, 3)
        self.provider = Provider.objects.create(company=self.company, name="Perfiles SA")

    def _pay(self, url_name, pk, amount):
        return self._post(url_name, pk, data=payment_form(amount=amount))

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
        response = self._pay("admin:invoices_invoice_register_payment", invoice.pk, "1210.00")
        self.assertEqual(response.status_code, 302)

        payment = Payment.objects.get()
        self.assertEqual(payment.direction, PaymentDirection.OUTBOUND)
        self.assertEqual(payment.amount, Money(Decimal("1210.00"), "EUR"))
        self.assertEqual(PaymentAllocation.objects.get().related_object, invoice)

        invoice.refresh_from_db()
        self.assertEqual(invoice.payment_status, InvoicePaymentStatus.PAID)

    def test_overpaying_a_purchase_invoice_asks_where_the_surplus_goes(self):
        # No cap is declared for invoices, unlike expense claims, but the
        # excess is not saved until the user confirms the distribution step.
        invoice = self._purchase_invoice()
        response = self._pay("admin:invoices_invoice_register_payment", invoice.pk, "2000.00")
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "admin/payments/distribute_surplus.html")
        self.assertEqual(response.context["surplus"], Money("790.00", "EUR"))
        self.assertFalse(Payment.objects.exists())

        response = self._pay("admin:invoices_invoice_distribute_surplus", invoice.pk, "2000.00")
        self.assertEqual(response.status_code, 302)

        payment = Payment.objects.get()
        self.assertEqual(payment.amount, Money(Decimal("2000.00"), "EUR"))
        self.assertEqual(PaymentAllocation.objects.get().amount_applied, Money(Decimal("1210.00"), "EUR"))
        invoice.refresh_from_db()
        self.assertEqual(invoice.payment_status, InvoicePaymentStatus.PAID)

    # ── Sales invoice: money comes in ────────────────────────────────────────

    def _sales_invoice(self):
        return SalesInvoice.objects.create(
            company=self.company,
            client=Client.objects.create(company=self.company, name="Cliente SA"),
            invoice_number="V-001",
            status=SalesInvoiceStatus.FINAL,
            date_issued=self.today,
            due_date=self.today + timedelta(days=30),
            total_amount=Money("100.00", "EUR"),
        )

    def test_paying_a_sales_invoice_creates_an_inbound_payment(self):
        invoice = self._sales_invoice()
        response = self._pay("admin:invoices_salesinvoice_register_payment", invoice.pk, "50.00")
        self.assertEqual(response.status_code, 302)

        payment = Payment.objects.get()
        self.assertEqual(payment.direction, PaymentDirection.INBOUND)
        self.assertEqual(payment.client, invoice.client)
        self.assertEqual(PaymentAllocation.objects.get().related_object, invoice)


class PaymentDeletionRevertsInvoiceStatusTest(CompanyAdminTestMixin, TestCase):
    """
    Deleting a Payment cascades the deletion of its PaymentAllocation rows
    through Django's collector, which bypasses PaymentAllocation.delete().
    The post_delete signal in payments/models.py must catch that case too.
    """

    def setUp(self):
        super().setUp()
        self.today = date(2026, 7, 3)

    def test_deleting_the_payment_reverts_a_fully_paid_purchase_invoice_to_unpaid(self):
        provider = Provider.objects.create(company=self.company, name="Perfiles SA")
        invoice = Invoice.objects.create(
            company=self.company,
            provider=provider,
            invoice_number="F-002",
            date_issued=self.today,
            total_amount=Money("1000.00", "EUR"),
        )
        payment = Payment.objects.create(
            company=self.company,
            payment_date=self.today,
            amount=Money("1000.00", "EUR"),
            direction=PaymentDirection.OUTBOUND,
            method=PaymentMethod.CASH,
        )
        PaymentAllocation.objects.create(
            payment=payment, amount_applied=Money("1000.00", "EUR"), related_object=invoice
        )
        invoice.refresh_from_db()
        self.assertEqual(invoice.payment_status, InvoicePaymentStatus.PAID)

        payment.delete()

        invoice.refresh_from_db()
        self.assertEqual(invoice.payment_status, InvoicePaymentStatus.UNPAID)

    def test_deleting_one_of_two_payments_reverts_a_sales_invoice_to_partial(self):
        client = Client.objects.create(company=self.company, name="Cliente SA")
        invoice = SalesInvoice.objects.create(
            company=self.company,
            client=client,
            invoice_number="V-002",
            status=SalesInvoiceStatus.FINAL,
            date_issued=self.today,
            total_amount=Money("1000.00", "EUR"),
        )
        first_payment = Payment.objects.create(
            company=self.company,
            payment_date=self.today,
            amount=Money("600.00", "EUR"),
            direction=PaymentDirection.INBOUND,
            method=PaymentMethod.CASH,
        )
        second_payment = Payment.objects.create(
            company=self.company,
            payment_date=self.today,
            amount=Money("400.00", "EUR"),
            direction=PaymentDirection.INBOUND,
            method=PaymentMethod.CASH,
        )
        PaymentAllocation.objects.create(
            payment=first_payment, amount_applied=Money("600.00", "EUR"), related_object=invoice
        )
        PaymentAllocation.objects.create(
            payment=second_payment, amount_applied=Money("400.00", "EUR"), related_object=invoice
        )
        invoice.refresh_from_db()
        self.assertEqual(invoice.payment_status, InvoicePaymentStatus.PAID)

        second_payment.delete()

        invoice.refresh_from_db()
        self.assertEqual(invoice.payment_status, InvoicePaymentStatus.PARTIAL)
