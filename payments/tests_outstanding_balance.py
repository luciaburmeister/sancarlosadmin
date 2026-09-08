"""Tests for the overdue / not-yet-due split of a counterparty's balance.

Covers the annotations in `payments.mixins` and the Client and Provider
changelists that show them (`payments.admin_mixins`).
"""
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone
from djmoney.money import Money

from company.models import Company
from invoices.models import (
    Invoice,
    InvoicePaymentStatus,
    SalesInvoice,
    SalesInvoiceStatus,
)
from materials.models import Provider
from payments.mixins import (
    annotate_balance_currency,
    annotate_outstanding_balance,
    annotate_split_outstanding_balance,
    currency_symbol,
    pending_invoices_queryset,
)
from payments.models import Payment, PaymentAllocation, PaymentDirection, PaymentMethod
from sales.models import Client

User = get_user_model()


class SplitOutstandingBalanceTest(TestCase):
    """The split is driven by due_date; the two halves must reconstruct the total."""

    def setUp(self):
        self.company = Company.objects.create(name="Global Corp")
        self.today = timezone.localdate()
        self.provider = Provider.objects.create(company=self.company, name="Perfiles SA")

    def _invoice(self, number, total, due_date, **kwargs):
        return Invoice.objects.create(
            company=self.company,
            provider=self.provider,
            invoice_number=number,
            date_issued=self.today - timedelta(days=60),
            due_date=due_date,
            total_amount=Money(total, "EUR"),
            **kwargs,
        )

    def _annotated_provider(self):
        return annotate_split_outstanding_balance(
            Provider.objects.filter(pk=self.provider.pk), Invoice, "provider"
        ).get()

    def test_overdue_and_not_due_are_split_on_due_date(self):
        self._invoice("F-OLD", "100.00", self.today - timedelta(days=1))
        self._invoice("F-NEW", "250.00", self.today + timedelta(days=10))

        provider = self._annotated_provider()
        self.assertEqual(provider.balance_overdue, Decimal("100.00"))
        self.assertEqual(provider.balance_not_due, Decimal("250.00"))

    def test_invoice_due_today_is_not_overdue_yet(self):
        self._invoice("F-TODAY", "80.00", self.today)

        provider = self._annotated_provider()
        self.assertEqual(provider.balance_overdue, Decimal("0.00"))
        self.assertEqual(provider.balance_not_due, Decimal("80.00"))

    def test_invoice_without_due_date_counts_as_not_due(self):
        self._invoice("F-NODATE", "60.00", None)

        provider = self._annotated_provider()
        self.assertEqual(provider.balance_overdue, Decimal("0.00"))
        self.assertEqual(provider.balance_not_due, Decimal("60.00"))

    def test_paid_invoices_are_excluded_from_both_halves(self):
        self._invoice(
            "F-PAID", "500.00", self.today - timedelta(days=5),
            payment_status=InvoicePaymentStatus.PAID,
        )

        provider = self._annotated_provider()
        self.assertEqual(provider.balance_overdue, Decimal("0.00"))
        self.assertEqual(provider.balance_not_due, Decimal("0.00"))

    def test_partial_payments_only_leave_the_remainder_overdue(self):
        invoice = self._invoice("F-PART", "300.00", self.today - timedelta(days=3))
        payment = Payment.objects.create(
            company=self.company,
            amount=Money("120.00", "EUR"),
            payment_date=self.today,
            direction=PaymentDirection.OUTBOUND,
            method=PaymentMethod.TRANSFER,
        )
        PaymentAllocation.objects.create(
            payment=payment, related_object=invoice, amount_applied=Decimal("120.00")
        )

        provider = self._annotated_provider()
        self.assertEqual(provider.balance_overdue, Decimal("180.00"))

    def test_halves_add_up_to_the_undivided_balance(self):
        self._invoice("F-1", "100.00", self.today - timedelta(days=1))
        self._invoice("F-2", "250.00", self.today + timedelta(days=10))
        self._invoice("F-3", "70.00", None)

        provider = self._annotated_provider()
        total = annotate_outstanding_balance(
            Provider.objects.filter(pk=self.provider.pk), Invoice, "provider", "balance_owed"
        ).get()
        self.assertEqual(
            provider.balance_overdue + provider.balance_not_due, total.balance_owed
        )


class ClientBalanceFiltersTest(TestCase):
    """Sales invoices only count once they are final, on both sides of the split."""

    def setUp(self):
        self.company = Company.objects.create(name="Global Corp")
        self.today = timezone.localdate()
        self.client_obj = Client.objects.create(company=self.company, name="Cliente SA")

    def _sales_invoice(self, number, total, due_date, status):
        invoice = SalesInvoice.objects.create(
            company=self.company,
            client=self.client_obj,
            invoice_number=number,
            status=status,
            date_issued=self.today - timedelta(days=30),
            due_date=due_date,
            total_amount=Money(total, "EUR"),
        )
        if due_date is None:
            # Internal invoices derive a due date from the client's payment
            # terms on creation, so clear it to model an undated row.
            SalesInvoice.objects.filter(pk=invoice.pk).update(due_date=None)
            invoice.refresh_from_db()
        return invoice

    def test_proforma_invoices_are_ignored(self):
        self._sales_invoice(
            "V-PRO", "400.00", self.today - timedelta(days=5), SalesInvoiceStatus.PROFORMA
        )
        self._sales_invoice(
            "V-FIN", "100.00", self.today - timedelta(days=5), SalesInvoiceStatus.FINAL
        )

        client = annotate_split_outstanding_balance(
            Client.objects.filter(pk=self.client_obj.pk),
            SalesInvoice,
            "client",
            invoice_filters={"status": SalesInvoiceStatus.FINAL},
        ).get()
        self.assertEqual(client.balance_overdue, Decimal("100.00"))
        self.assertEqual(client.balance_not_due, Decimal("0.00"))

    def test_pending_invoices_queryset_flags_overdue_and_orders_by_due_date(self):
        later = self._sales_invoice(
            "V-LATER", "50.00", self.today + timedelta(days=10), SalesInvoiceStatus.FINAL
        )
        earlier = self._sales_invoice(
            "V-EARLIER", "80.00", self.today - timedelta(days=2), SalesInvoiceStatus.FINAL
        )
        undated = self._sales_invoice(
            "V-UNDATED", "20.00", None, SalesInvoiceStatus.FINAL
        )

        invoices = list(
            pending_invoices_queryset(
                SalesInvoice, {"status": SalesInvoiceStatus.FINAL}
            )
        )
        self.assertEqual([i.pk for i in invoices], [earlier.pk, later.pk, undated.pk])
        self.assertEqual([i.is_overdue for i in invoices], [True, False, False])
        self.assertEqual(invoices[0].db_amount_remaining, Decimal("80.00"))


class CounterpartyChangelistTest(TestCase):
    """The columns and the dropdown both render on the two admin screens."""

    def setUp(self):
        self.company = Company.objects.create(name="Global Corp")
        self.today = timezone.localdate()
        self.user = User.objects.create_superuser(
            username="finance", password="pw", email="fin@example.com"
        )
        self.client.force_login(self.user)

    def test_provider_changelist_shows_split_and_pending_invoice(self):
        provider = Provider.objects.create(company=self.company, name="Perfiles SA")
        for number, due_date in [
            ("F-LATE", self.today - timedelta(days=1)),
            ("F-SOON", self.today + timedelta(days=15)),
        ]:
            Invoice.objects.create(
                company=self.company,
                provider=provider,
                invoice_number=number,
                date_issued=self.today - timedelta(days=60),
                due_date=due_date,
                total_amount=Money("100.00", "EUR"),
            )

        response = self.client.get(reverse("admin:materials_provider_changelist"))
        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        self.assertIn("due_date_passed=yes", body)
        self.assertIn("due_date_passed=no", body)
        # The dropdown lists the invoice itself, not just the total.
        self.assertIn("F-LATE", body)
        # Total alongside the split: 100 overdue + 100 not due.
        self.assertIn("200.00 €", body)
        self.assertEqual(body.count("100.00 €"), 2)

    def test_dropdown_does_not_cost_a_query_per_row(self):
        # The section renders every row's invoices; without the prefetch in
        # get_queryset the query count would grow with the rows.
        url = reverse("admin:materials_provider_changelist")
        self._provider_with_invoice("Perfiles SA", "F-1")
        with CaptureQueriesContext(connection) as one_row:
            self.client.get(url)

        for i in range(5):
            self._provider_with_invoice(f"Extra {i}", f"F-EXTRA-{i}")
        with CaptureQueriesContext(connection) as six_rows:
            self.client.get(url)

        self.assertLessEqual(len(six_rows), len(one_row))

    def _provider_with_invoice(self, name, invoice_number):
        provider = Provider.objects.create(company=self.company, name=name)
        Invoice.objects.create(
            company=self.company,
            provider=provider,
            invoice_number=invoice_number,
            date_issued=self.today - timedelta(days=60),
            due_date=self.today - timedelta(days=1),
            total_amount=Money("100.00", "EUR"),
        )
        return provider

    def test_client_changelist_shows_split_and_pending_invoice(self):
        client_obj = Client.objects.create(company=self.company, name="Cliente SA")
        SalesInvoice.objects.create(
            company=self.company,
            client=client_obj,
            invoice_number="V-LATE",
            status=SalesInvoiceStatus.FINAL,
            date_issued=self.today - timedelta(days=60),
            due_date=self.today - timedelta(days=1),
            total_amount=Money("100.00", "EUR"),
        )

        response = self.client.get(reverse("admin:sales_client_changelist"))
        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        self.assertIn("V-LATE", body)
        # Sales invoices are filtered to final ones on the way to the invoice list.
        self.assertIn(f"status__exact={SalesInvoiceStatus.FINAL}", body)

    def test_non_eur_balance_shows_its_own_currency_not_the_euro_sign(self):
        provider = Provider.objects.create(company=self.company, name="US Supplier")
        Invoice.objects.create(
            company=self.company,
            provider=provider,
            invoice_number="F-USD",
            date_issued=self.today - timedelta(days=60),
            due_date=self.today - timedelta(days=1),
            total_amount=Money("100.00", "USD"),
        )

        response = self.client.get(reverse("admin:materials_provider_changelist"))
        body = response.content.decode()
        self.assertIn("100.00 $", body)
        self.assertNotIn("100.00 €", body)


class CurrencySymbolTest(TestCase):
    """Symbols come from settings.CURRENCY_CHOICES, not a hardcoded EUR special case."""

    def test_known_currencies_show_their_settings_symbol(self):
        self.assertEqual(currency_symbol("EUR"), "€")
        self.assertEqual(currency_symbol("USD"), "$")
        self.assertEqual(currency_symbol("GBP"), "£")

    def test_unlisted_currency_falls_back_to_its_code(self):
        self.assertEqual(currency_symbol("JPY"), "JPY")


class AnnotateBalanceCurrencyTest(TestCase):
    """The label on a summed balance should reflect what it was actually summed from."""

    def setUp(self):
        self.company = Company.objects.create(name="Global Corp")
        self.today = timezone.localdate()

    def test_reads_currency_off_the_outstanding_invoice(self):
        provider = Provider.objects.create(company=self.company, name="UK Supplier")
        Invoice.objects.create(
            company=self.company,
            provider=provider,
            invoice_number="F-GBP",
            date_issued=self.today,
            due_date=self.today + timedelta(days=30),
            total_amount=Money("50.00", "GBP"),
        )

        result = annotate_balance_currency(
            Provider.objects.filter(pk=provider.pk), Invoice, "provider"
        ).get()
        self.assertEqual(result.balance_currency, "GBP")

    def test_defaults_to_eur_with_no_outstanding_invoices(self):
        provider = Provider.objects.create(company=self.company, name="No Debt SA")

        result = annotate_balance_currency(
            Provider.objects.filter(pk=provider.pk), Invoice, "provider"
        ).get()
        self.assertEqual(result.balance_currency, "EUR")
