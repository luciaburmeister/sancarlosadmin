"""The "Subtotal" shown for a sales invoice (admin, export) is the taxable
base: line totals net of the invoice-level discount. ``subtotal_amount``
itself keeps the raw line sum so the discount can be shown against it."""
from decimal import Decimal

from django.contrib.admin.sites import site
from django.test import TestCase

from company.models import Company
from invoices.admin import SalesInvoiceAdmin
from invoices.models import SalesInvoice, SalesInvoiceItem, SalesInvoiceStatus
from invoices.resources import SalesInvoiceResource
from sales.models import Client


class SalesInvoiceSubtotalAfterDiscountTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Test Co")
        self.client_obj = Client.objects.create(name="Acme", company=self.company)
        self.invoice = SalesInvoice.objects.create(
            company=self.company,
            client=self.client_obj,
            status=SalesInvoiceStatus.PROFORMA,
            discount_amount=Decimal("14.61"),
        )
        SalesInvoiceItem.objects.create(
            invoice=self.invoice,
            description="Line",
            quantity=Decimal("1"),
            unit_price=Decimal("792.69"),
        )
        self.invoice.recalculate_totals()

    def test_taxable_base_is_lines_total_minus_discount(self):
        self.assertEqual(self.invoice.subtotal_amount.amount, Decimal("792.69"))
        self.assertEqual(self.invoice.taxable_base.amount, Decimal("778.08"))
        self.assertEqual(str(self.invoice.taxable_base.currency), "EUR")

    def test_taxable_base_follows_invoice_currency(self):
        self.invoice.currency = "USD"
        self.assertEqual(str(self.invoice.taxable_base.currency), "USD")

    def test_admin_financial_summary_shows_discounted_subtotal(self):
        admin = SalesInvoiceAdmin(SalesInvoice, site)

        self.assertEqual(admin.taxable_base_display(self.invoice).amount, Decimal("778.08"))
        self.assertEqual(admin.lines_total_display(self.invoice).amount, Decimal("792.69"))

        financial_fields = [
            f for name, opts in admin.fieldsets if str(name) == "Financial Summary"
            for group in opts["fields"]
            for f in (group if isinstance(group, tuple) else (group,))
        ]
        self.assertIn("taxable_base_display", financial_fields)
        self.assertIn("lines_total_display", financial_fields)
        self.assertNotIn("subtotal_amount", financial_fields)
        # Discount is listed before the subtotal it produces.
        self.assertLess(
            financial_fields.index("discount_amount"),
            financial_fields.index("taxable_base_display"),
        )

    def test_export_subtotal_column_is_after_discount(self):
        resource = SalesInvoiceResource()
        dataset = resource.export(queryset=SalesInvoice.objects.filter(pk=self.invoice.pk))

        headers = list(dataset.headers)
        row = dict(zip(headers, dataset[0]))
        subtotal_header = str(resource.fields["subtotal_amount_value"].column_name)
        discount_header = str(resource.fields["discount_amount_value"].column_name)

        self.assertEqual(Decimal(str(row[subtotal_header])), Decimal("778.08"))
        self.assertEqual(Decimal(str(row[discount_header])), Decimal("14.61"))
        self.assertLess(headers.index(discount_header), headers.index(subtotal_header))
