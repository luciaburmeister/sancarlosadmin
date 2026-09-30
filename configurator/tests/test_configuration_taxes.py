"""
A configuration's taxes each apply to its taxable base, as invoices apply
them, retentions subtracted: IVA 21 % and IRPF 15 % on 1,000.00 come to
1,000 + 210 − 150 = 1,060.00 for the configuration, its quote and the
invoice built from the expediente — never IRPF on the base plus the IVA.
"""
from datetime import date
from decimal import Decimal

from django.contrib.contenttypes.models import ContentType
from django.test import TestCase
from djmoney.money import Money

from company.models import Company
from configurator.models import Configuration, ConfigurationStatus, ProductTemplate
from invoices.models import AppliedTax, TaxType
from invoices.services.project_file_invoice_service import ProjectFileInvoiceService
from management.models import ProjectFile
from sales.models import Client, Quote, QuoteStatus


class ConfigurationTaxesTest(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Retenciones SL")
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
            project_file=self.project_file,
            status=ConfigurationStatus.DRAFT,
            price_override=Money(Decimal("1000.00"), "EUR"),
        )
        for order, (name, behavior, percentage) in enumerate(
            (("IVA", "add", "21"), ("IRPF", "subtract", "15"))
        ):
            tax_type, _created = TaxType.objects.get_or_create(
                tax_name=name, country="ES", defaults={"behavior": behavior},
            )
            AppliedTax.objects.create(
                content_type=ContentType.objects.get_for_model(Configuration),
                object_id=self.config.pk,
                tax_type=tax_type,
                selected_percentage=Decimal(percentage),
                order=order,
            )
        self.config.recalculate_price()
        self.config.refresh_from_db()
        self.quote.recalc_totals()
        self.quote.refresh_from_db()

    def test_every_tax_is_on_the_taxable_base(self):
        self.assertEqual(self.config.total_price.amount, Decimal("1060.00"))
        self.assertEqual(
            [(entry["base"], entry["tax_amount"]) for entry in self.config.tax_breakdown],
            [("1000.00", "210.00"), ("1000.00", "150.00")],
        )
        self.assertEqual(
            sorted(
                (tax.tax_type.tax_name, tax.base_amount.amount, tax.calculated_amount.amount)
                for tax in self.config.get_taxes()
            ),
            [("IRPF", Decimal("1000.00"), Decimal("150.00")), ("IVA", Decimal("1000.00"), Decimal("210.00"))],
        )

    def test_quote_and_invoice_agree(self):
        self.assertEqual(self.quote.total_price.amount, Decimal("1060.00"))

        summary = ProjectFileInvoiceService.get_invoiceable_summary(self.project_file)
        invoice = ProjectFileInvoiceService.create_invoice_from_project_file(
            project_file=self.project_file, company=self.company, client=self.client_obj,
            date_issued=date(2026, 9, 29), due_date=None,
            items_data=ProjectFileInvoiceService.build_items_for_ratio(summary, Decimal("1")),
        )

        self.assertEqual(invoice.total_amount.amount, self.quote.total_price.amount)
