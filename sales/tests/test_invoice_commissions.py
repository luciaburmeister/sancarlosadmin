"""
The commissions list: one row per invoice and agent. Invoices no quote
accounts for (imported, direct sales) carry the client's agent's commission.
"""
from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.urls import reverse
from djmoney.money import Money

from configurator.commission.admin import InvoiceCommissionResource
from configurator.commission.models import CommissionRelease, InvoiceCommission
from configurator.commission.services import CommissionReleaseService
from invoices.models import InvoicePaymentStatus, SalesInvoice, SalesInvoiceItem, SalesInvoiceStatus
from sales.models import Client
from sales.tests.test_commission_releases import _CommissionReleaseTestCase


class _ClientAgentTestCase(_CommissionReleaseTestCase):
    """The client's agent (10 % rule) also covers invoices with no quote."""

    def setUp(self):
        super().setUp()
        Client.objects.filter(pk=self.client_obj.pk).update(agent=self.agent)
        self.client_obj.refresh_from_db()

    def plain_invoice(self, amount, date_issued=date(2026, 9, 10), **kwargs):
        """An invoice like the imported ones: one line with no source, issued."""
        invoice = SalesInvoice.objects.create(
            company=self.company, client=self.client_obj,
            status=SalesInvoiceStatus.PROFORMA, date_issued=date_issued, **kwargs,
        )
        SalesInvoiceItem.objects.create(
            invoice=invoice, description="Imported line item",
            quantity=Decimal("1"), unit_price=Money(amount, "EUR"),
        )
        invoice.recalculate_totals()
        self.set_status(invoice, SalesInvoiceStatus.FINAL)
        return invoice

    def row(self, invoice):
        return InvoiceCommission.objects.get(sales_invoice=invoice)


class InvoiceCommissionTest(_ClientAgentTestCase):
    def test_invoice_without_quote_uses_the_clients_agent(self):
        invoice = self.plain_invoice(Decimal("500.00"))
        self.pay(invoice, invoice.total_amount.amount, date(2026, 9, 20))

        row = self.row(invoice)
        self.assertEqual(row.agent, self.agent)
        self.assertEqual(row.rule_value_snapshot, Decimal("10.00"))
        self.assertEqual(row.commission_amount.amount, Decimal("50.00"))
        self.assertEqual(row.released_amount.amount, Decimal("50.00"))
        self.assertEqual(row.release_date, date(2026, 9, 20))

    def test_paid_in_two_parts_is_one_row(self):
        invoice = self.plain_invoice(Decimal("1000.00"))
        self.pay(invoice, Decimal("400.00"), date(2026, 9, 5))
        self.pay(invoice, Decimal("600.00"), date(2026, 9, 25))

        row = self.row(invoice)
        self.assertEqual(row.releases.count(), 2)
        self.assertEqual(row.released_base_amount.amount, Decimal("1000.00"))
        self.assertEqual(row.released_amount.amount, Decimal("100.00"))
        self.assertEqual(row.release_date, date(2026, 9, 25))
        self.assertEqual(row.commission_percentage, Decimal("10.00"))

    def test_quote_invoice_row_adds_up_the_quote_commission(self):
        invoice = self.final_goods_invoice()
        self.pay(invoice, invoice.total_amount.amount)
        row = self.row(invoice)
        self.assertEqual(row.own_base_amount.amount, Decimal("0.00"))
        self.assertEqual(row.released_amount.amount, Decimal("100.00"))
        self.assertEqual(self.released(), Decimal("100.00"))

    def test_rule_is_frozen_once_issued(self):
        invoice = self.plain_invoice(Decimal("500.00"))
        self.rule.value = Decimal("20")
        self.rule.save()
        self.pay(invoice, invoice.total_amount.amount)
        self.assertEqual(self.row(invoice).released_amount.amount, Decimal("50.00"))

    def test_marked_paid_by_hand_releases_on_that_day(self):
        invoice = self.plain_invoice(Decimal("500.00"))
        invoice.payment_status = InvoicePaymentStatus.PAID
        invoice.save(update_fields=["payment_status"])

        row = self.row(invoice)
        self.assertEqual(row.released_amount.amount, Decimal("50.00"))
        self.assertEqual(row.release_date, date.today())

    def test_imported_already_paid_releases_on_its_issue_date(self):
        invoice = self.plain_invoice(
            Decimal("500.00"), date_issued=date(2025, 3, 4), payment_status=InvoicePaymentStatus.PAID,
        )
        CommissionReleaseService.sync_for_invoice(invoice)
        self.assertEqual(self.row(invoice).release_date, date(2025, 3, 4))

    def test_credit_note_takes_it_back_on_issue(self):
        credit_note = self.plain_invoice(Decimal("-200.00"), date_issued=date(2026, 9, 27))
        row = self.row(credit_note)
        self.assertEqual(row.released_amount.amount, Decimal("-20.00"))
        self.assertEqual(row.release_date, date(2026, 9, 27))

    def test_overpayment_releases_the_commission_once(self):
        invoice = self.plain_invoice(Decimal("500.00"))
        self.pay(invoice, Decimal("400.00"))
        self.pay(invoice, Decimal("400.00"))
        row = self.row(invoice)
        self.assertEqual(row.released_amount.amount, Decimal("50.00"))
        self.assertEqual(row.released_base_amount.amount, Decimal("500.00"))

    def test_cancelling_removes_the_row(self):
        invoice = self.plain_invoice(Decimal("500.00"))
        self.set_status(invoice, SalesInvoiceStatus.CANCELLED)
        self.assertFalse(InvoiceCommission.objects.filter(sales_invoice=invoice).exists())
        self.assertFalse(CommissionRelease.objects.filter(sales_invoice=invoice).exists())

    def test_client_without_agent_has_no_commission(self):
        Client.objects.filter(pk=self.client_obj.pk).update(agent=None)
        self.client_obj.refresh_from_db()
        invoice = self.plain_invoice(Decimal("500.00"))
        self.pay(invoice, invoice.total_amount.amount)
        self.assertFalse(InvoiceCommission.objects.filter(sales_invoice=invoice).exists())


class CommissionsAdminTest(_ClientAgentTestCase):
    def setUp(self):
        super().setUp()
        self.invoice = self.plain_invoice(Decimal("1000.00"), invoice_number="26FC-1")
        self.pay(self.invoice, Decimal("500.00"), date(2026, 9, 5))
        self.pay(self.invoice, Decimal("500.00"), date(2026, 9, 25))
        self.credit_note = self.plain_invoice(
            Decimal("-200.00"), date_issued=date(2026, 9, 27), invoice_number="26AB-1",
        )
        user = get_user_model().objects.create_superuser("commissions", "c@x.com", "pw")
        self.client.force_login(user)

    def test_changelist_lists_each_document_once(self):
        response = self.client.get(reverse("admin:configurator_invoicecommission_changelist"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Factura 26FC-1", count=1)
        self.assertContains(response, "Abono 26AB-1", count=1)
        self.assertContains(response, "Fecha factura")

    def test_export_has_the_report_columns(self):
        dataset = InvoiceCommissionResource().export(
            InvoiceCommission.objects.filter(release_date__month=9).order_by("release_date")
        )
        self.assertEqual(dataset.headers, [
            "Fecha de liberación", "Documento", "Fecha factura", "Cliente", "Base", "% Com.", "Comisión", "Agente",
        ])
        self.assertEqual(
            [row[1:7] for row in dataset],
            [
                ("Factura 26FC-1", "10/09/2026", "Cliente SA",
                 Decimal("1000.00"), Decimal("10.00"), Decimal("100.00")),
                ("Abono 26AB-1", "27/09/2026", "Cliente SA",
                 Decimal("-200.00"), Decimal("10.00"), Decimal("-20.00")),
            ],
        )
