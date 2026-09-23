"""Tests for Payment.client and the client's credit ("saldo a favor").

Covers how a payment learns its client from the sales invoice it is allocated
to, the one-off backfill of existing payments, and the `credit_balance`
annotation the Client changelist shows.
"""
from datetime import timedelta
from decimal import Decimal
from importlib import import_module

from django.apps import apps
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from djmoney.money import Money

from company.models import Company
from invoices.models import SalesInvoice, SalesInvoiceStatus
from payments.mixins import annotate_credit_balance
from payments.models import (
    Payment,
    PaymentAllocation,
    PaymentDirection,
    PaymentMethod,
    PaymentStatus,
)
from sales.models import Client

User = get_user_model()

backfill_payment_client = import_module(
    "payments.migrations.0006_backfill_payment_client"
).backfill_payment_client


class _ClientPaymentsMixin:
    def setUp(self):
        self.company = Company.objects.create(name="Global Corp")
        self.today = timezone.localdate()
        self.client_a = Client.objects.create(company=self.company, name="Cliente A")
        self.client_b = Client.objects.create(company=self.company, name="Cliente B")

    def _sales_invoice(self, client, number, total="100.00"):
        return SalesInvoice.objects.create(
            company=self.company,
            client=client,
            invoice_number=number,
            status=SalesInvoiceStatus.FINAL,
            date_issued=self.today - timedelta(days=30),
            due_date=self.today + timedelta(days=30),
            total_amount=Money(total, "EUR"),
        )

    def _payment(self, amount="100.00", **kwargs):
        defaults = dict(
            company=self.company,
            amount=Money(amount, "EUR") if isinstance(amount, str) else amount,
            payment_date=self.today,
            direction=PaymentDirection.INBOUND,
            method=PaymentMethod.CASH,
            status=PaymentStatus.CONFIRMED,
        )
        defaults.update(kwargs)
        return Payment.objects.create(**defaults)

    def _allocate(self, payment, invoice, amount):
        return PaymentAllocation.objects.create(
            payment=payment, related_object=invoice, amount_applied=Money(amount, "EUR")
        )


class PaymentClientFromAllocationTest(_ClientPaymentsMixin, TestCase):
    """A payment takes its client from the first sales invoice it is applied to."""

    def test_allocating_to_a_sales_invoice_sets_the_client(self):
        invoice = self._sales_invoice(self.client_a, "V-1")
        payment = self._payment()

        self._allocate(payment, invoice, "100.00")

        payment.refresh_from_db()
        self.assertEqual(payment.client, self.client_a)

    def test_an_explicit_client_is_not_overridden(self):
        # Someone recorded it against client B on purpose; an allocation to
        # A's invoice (e.g. a group paying for a subsidiary) must not flip it.
        invoice = self._sales_invoice(self.client_a, "V-1")
        payment = self._payment(client=self.client_b)

        self._allocate(payment, invoice, "100.00")

        payment.refresh_from_db()
        self.assertEqual(payment.client, self.client_b)

    def test_new_payment_without_allocations_is_fully_pending(self):
        # A payment on account has nothing to allocate to yet, so all of it is
        # pending: the field must not sit at its 0 default.
        payment = self._payment("250.00")

        payment.refresh_from_db()
        self.assertEqual(payment.amount_pending_allocation, Money("250.00", "EUR"))

    def test_register_payment_screen_records_the_client(self):
        user = User.objects.create_superuser(username="fin", password="pw", email="f@example.com")
        self.client.force_login(user)
        invoice = self._sales_invoice(self.client_a, "V-1")

        from django.utils import translation
        with translation.override("en"):
            response = self.client.post(
                reverse("admin:invoices_salesinvoice_register_payment", args=[invoice.pk]),
                {
                    "amount": "100.00",
                    "currency": "EUR",
                    "payment_date": self.today.isoformat(),
                    "method": PaymentMethod.TRANSFER,
                    "reference": "REF-1",
                },
            )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(Payment.objects.get().client, self.client_a)


class BackfillPaymentClientTest(_ClientPaymentsMixin, TestCase):
    """The migration derives the client from existing allocations, never guesses."""

    def _detached_payment(self, invoices, amounts):
        """A payment allocated before the field existed: client cleared afterwards."""
        payment = self._payment(str(sum(Decimal(a) for a in amounts)))
        for invoice, amount in zip(invoices, amounts):
            self._allocate(payment, invoice, amount)
        Payment.objects.filter(pk=payment.pk).update(client=None)
        return payment

    def test_fills_client_from_sales_invoice_allocations(self):
        payment = self._detached_payment(
            [self._sales_invoice(self.client_a, "V-1"), self._sales_invoice(self.client_a, "V-2")],
            ["60.00", "40.00"],
        )

        backfill_payment_client(apps, None)

        payment.refresh_from_db()
        self.assertEqual(payment.client, self.client_a)

    def test_payment_split_across_two_clients_is_left_empty(self):
        payment = self._detached_payment(
            [self._sales_invoice(self.client_a, "V-1"), self._sales_invoice(self.client_b, "V-2")],
            ["50.00", "50.00"],
        )

        backfill_payment_client(apps, None)

        payment.refresh_from_db()
        self.assertIsNone(payment.client)

    def test_payment_without_sales_invoice_allocation_is_untouched(self):
        payment = self._payment()

        backfill_payment_client(apps, None)

        payment.refresh_from_db()
        self.assertIsNone(payment.client)

    def test_already_assigned_client_is_kept(self):
        invoice = self._sales_invoice(self.client_a, "V-1")
        payment = self._payment(client=self.client_b)
        self._allocate(payment, invoice, "100.00")

        backfill_payment_client(apps, None)

        payment.refresh_from_db()
        self.assertEqual(payment.client, self.client_b)


class ClientCreditBalanceTest(_ClientPaymentsMixin, TestCase):
    """Credit is what the client paid us that no invoice has consumed."""

    def _credit(self, client):
        return annotate_credit_balance(
            Client.objects.filter(pk=client.pk), "client", PaymentDirection.INBOUND
        ).get()

    def test_payment_on_account_counts_in_full(self):
        self._payment("300.00", client=self.client_a)

        self.assertEqual(self._credit(self.client_a).credit_balance, Decimal("300.00"))

    def test_partially_allocated_payment_counts_its_remainder(self):
        invoice = self._sales_invoice(self.client_a, "V-1", "80.00")
        payment = self._payment("300.00")
        self._allocate(payment, invoice, "80.00")

        self.assertEqual(self._credit(self.client_a).credit_balance, Decimal("220.00"))

    def test_fully_allocated_payment_adds_nothing(self):
        invoice = self._sales_invoice(self.client_a, "V-1", "100.00")
        self._allocate(self._payment("100.00"), invoice, "100.00")

        self.assertEqual(self._credit(self.client_a).credit_balance, Decimal("0.00"))

    def test_draft_cancelled_and_outbound_payments_are_ignored(self):
        self._payment("50.00", client=self.client_a, status=PaymentStatus.DRAFT)
        self._payment("50.00", client=self.client_a, status=PaymentStatus.CANCELLED)
        # A refund we sent back is money leaving, not credit they hold with us.
        self._payment("50.00", client=self.client_a, direction=PaymentDirection.OUTBOUND)

        self.assertEqual(self._credit(self.client_a).credit_balance, Decimal("0.00"))

    def test_other_clients_payments_do_not_leak(self):
        self._payment("500.00", client=self.client_b)

        self.assertEqual(self._credit(self.client_a).credit_balance, Decimal("0.00"))
        self.assertEqual(self._credit(self.client_a).credit_balance_currency, "EUR")

    def test_currency_comes_off_the_summed_payment(self):
        self._payment(client=self.client_a, amount=Money("40.00", "USD"))

        credit = self._credit(self.client_a)
        self.assertEqual(credit.credit_balance, Decimal("40.00"))
        self.assertEqual(credit.credit_balance_currency, "USD")


class ClientChangelistCreditTest(_ClientPaymentsMixin, TestCase):
    def setUp(self):
        super().setUp()
        user = User.objects.create_superuser(username="fin", password="pw", email="f@example.com")
        self.client.force_login(user)

    def test_changelist_shows_credit_linking_to_the_unallocated_payments(self):
        self._payment("300.00", client=self.client_a)

        response = self.client.get(reverse("admin:sales_client_changelist"))

        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        self.assertIn("300.00 €", body)
        self.assertIn(f"client__id__exact={self.client_a.pk}&amp;unallocated=yes", body)

    def test_payment_changelist_unallocated_filter_matches_the_credit(self):
        pending = self._payment("300.00", client=self.client_a)
        invoice = self._sales_invoice(self.client_a, "V-1", "100.00")
        settled = self._payment("100.00")
        self._allocate(settled, invoice, "100.00")

        response = self.client.get(
            reverse("admin:payments_payment_changelist"),
            {"client__id__exact": self.client_a.pk, "unallocated": "yes"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [p.pk for p in response.context["cl"].result_list], [pending.pk]
        )
