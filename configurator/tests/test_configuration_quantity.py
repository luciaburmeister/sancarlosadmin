"""Configuration.quantity: one piece is priced, the stored totals are for
all of them, so quotes, delivery orders and invoices that sum configuration
totals need no change."""
from decimal import Decimal

from django.contrib.contenttypes.models import ContentType
from django.test import TestCase
from djmoney.money import Money

from company.models import Company
from configurator.models import Configuration, ConfigurationStatus, ProductTemplate
from invoices.models import AppliedTax, TaxType
from management.models import ProjectFile
from sales.models import Client, Quote, QuoteStatus


class ConfigurationQuantityPricingTest(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Piezas SL")
        self.client_obj = Client.objects.create(name="Cliente SA", company=self.company)
        self.project_file = ProjectFile.objects.create(
            name="Obra", company=self.company, client=self.client_obj,
        )
        self.quote = Quote.objects.create(
            project_name="Obra", company=self.company, client=self.client_obj,
            project_file=self.project_file, status=QuoteStatus.ACCEPTED,
        )
        self.config = Configuration.objects.create(
            name="Ventana",
            company=self.company,
            product=ProductTemplate.objects.create(name="Ventana", company=self.company),
            client=self.client_obj,
            quote=self.quote,
            status=ConfigurationStatus.DRAFT,
            price_override=Money(Decimal("100.00"), "EUR"),
            quantity=3,
        )
        tax_type, _created = TaxType.objects.get_or_create(
            tax_name="IVA", country="ES", defaults={"behavior": "add"},
        )
        AppliedTax.objects.create(
            content_type=ContentType.objects.get_for_model(Configuration),
            object_id=self.config.pk,
            tax_type=tax_type,
            selected_percentage=Decimal("21"),
            order=0,
        )
        self.config.recalculate_price()
        self.config.refresh_from_db()
        self.quote.refresh_from_db()

    def test_override_is_per_piece_and_totals_cover_every_piece(self):
        self.assertEqual(self.config.subtotal_price.amount, Decimal("300.00"))
        self.assertEqual(self.config.total_price.amount, Decimal("363.00"))

    def test_breakdown_records_the_piece_and_the_line(self):
        breakdown = self.config.pricing_breakdown
        self.assertEqual(breakdown["quantity"], 3)
        self.assertEqual(breakdown["piece_subtotal"], "100.00")
        self.assertEqual(breakdown["post_discount_subtotal"], "300.00")
        self.assertEqual([line["amount"] for line in breakdown["lines"]], ["300.00"])

    def test_quote_totals_include_every_piece(self):
        self.assertEqual(self.quote.subtotal_price.amount, Decimal("300.00"))

    def test_without_a_unit_variable_lines_carry_the_piece_count(self):
        self.assertEqual(self.config.units, Decimal("0"))
        self.assertEqual(self.config.invoice_quantity, Decimal("3"))

    def test_single_piece_still_bills_one(self):
        self.config.quantity = 1
        self.config.save(update_fields=["quantity"])
        self.config.recalculate_price()
        self.config.refresh_from_db()

        self.assertEqual(self.config.subtotal_price.amount, Decimal("100.00"))
        self.assertEqual(self.config.invoice_quantity, Decimal("1"))

    def test_clone_keeps_the_quantity(self):
        self.assertEqual(self.config.clone().quantity, 3)
