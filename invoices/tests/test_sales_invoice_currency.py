"""
Tests for the currency of a sales invoice.

``SalesInvoice.currency`` is the source of truth: every Money on the invoice,
its line items and its applied taxes carries it, recalculating totals keeps it,
and switching it re-labels the children.
"""
from decimal import Decimal

from django.contrib.contenttypes.models import ContentType
from django.test import TestCase
from djmoney.money import Money

from company.models import Company
from invoices.models import (
    AppliedTax,
    SalesInvoice,
    SalesInvoiceItem,
    TaxType,
)
from sales.models import Client


class SalesInvoiceCurrencyTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Test Co")
        self.client_obj = Client.objects.create(name="Acme", company=self.company)
        self.tax_type, _ = TaxType.objects.get_or_create(
            tax_name="VAT", country="ES", defaults={"behavior": "add"}
        )

    def _invoice(self, **kwargs):
        kwargs.setdefault("company", self.company)
        kwargs.setdefault("client", self.client_obj)
        return SalesInvoice.objects.create(**kwargs)

    def _add_tax(self, invoice, percentage=Decimal("21")):
        return AppliedTax.objects.create(
            content_type=ContentType.objects.get_for_model(invoice),
            object_id=invoice.pk,
            tax_type=self.tax_type,
            selected_percentage=percentage,
        )

    def test_defaults_to_eur(self):
        invoice = self._invoice()

        self.assertEqual(invoice.currency, "EUR")
        self.assertEqual(str(invoice.total_amount.currency), "EUR")

    def test_items_and_totals_follow_the_invoice_currency(self):
        invoice = self._invoice(currency="USD")
        SalesInvoiceItem.objects.create(
            invoice=invoice,
            description="Panel",
            quantity=Decimal("2"),
            unit_price=Decimal("100.00"),
            discount_percentage=Decimal("10"),
        )
        self._add_tax(invoice)

        item = invoice.items.get()
        self.assertEqual(str(item.unit_price.currency), "USD")
        self.assertEqual(str(item.discount_amount.currency), "USD")
        self.assertEqual(str(item.line_total.currency), "USD")

        invoice.recalculate_totals(save=True)
        invoice.refresh_from_db()

        self.assertEqual(invoice.subtotal_amount, Money("180.00", "USD"))
        self.assertEqual(invoice.tax_amount, Money("37.80", "USD"))
        self.assertEqual(invoice.total_amount, Money("217.80", "USD"))
        self.assertEqual(invoice.subtotal_amount_currency, "USD")
        self.assertEqual(invoice.tax_amount_currency, "USD")
        self.assertEqual(invoice.total_amount_currency, "USD")

        tax = AppliedTax.objects.get(object_id=invoice.pk)
        self.assertEqual(str(tax.base_amount.currency), "USD")
        self.assertEqual(str(tax.calculated_amount.currency), "USD")

    def test_a_line_priced_in_another_currency_is_relabelled(self):
        invoice = self._invoice(currency="GBP")
        SalesInvoiceItem.objects.create(
            invoice=invoice,
            description="Panel",
            quantity=Decimal("1"),
            unit_price=Money("50.00", "USD"),
        )

        item = invoice.items.get()
        self.assertEqual(item.unit_price, Money("50.00", "GBP"))
        self.assertEqual(item.line_total, Money("50.00", "GBP"))

    def test_switching_the_currency_relabels_items_and_taxes(self):
        invoice = self._invoice()
        SalesInvoiceItem.objects.create(
            invoice=invoice,
            description="Panel",
            quantity=Decimal("1"),
            unit_price=Decimal("100.00"),
        )
        self._add_tax(invoice)
        invoice.recalculate_totals(save=True)

        invoice = SalesInvoice.objects.get(pk=invoice.pk)
        invoice.currency = "USD"
        invoice.save()

        invoice.refresh_from_db()
        self.assertEqual(invoice.total_amount, Money("121.00", "USD"))
        self.assertEqual(str(invoice.items.get().unit_price.currency), "USD")

        tax = AppliedTax.objects.get(object_id=invoice.pk)
        self.assertEqual(str(tax.base_amount.currency), "USD")
        self.assertEqual(str(tax.calculated_amount.currency), "USD")

    def test_new_invoice_adopts_the_currency_of_the_money_it_was_built_from(self):
        """Created straight from a non-EUR Money without naming the currency."""
        invoice = self._invoice(total_amount=Money("500.00", "USD"))

        self.assertEqual(invoice.currency, "USD")
        self.assertEqual(invoice.total_amount_currency, "USD")

    def test_an_explicit_currency_wins_over_the_money_it_was_built_from(self):
        invoice = self._invoice(currency="GBP", total_amount=Money("500.00", "USD"))

        self.assertEqual(invoice.currency, "GBP")
        self.assertEqual(invoice.total_amount, Money("500.00", "GBP"))

    def test_partial_save_writes_the_currency_column_too(self):
        invoice = self._invoice(currency="USD")

        invoice.subtotal_amount = Decimal("42.00")
        invoice.save(update_fields=["subtotal_amount"])

        invoice.refresh_from_db()
        self.assertEqual(invoice.subtotal_amount, Money("42.00", "USD"))
