"""Full mode (build_items_for_remaining) bills each source's exact pending
amount, so invoicing "everything pending" closes the expediente at 0 and
carries the quote's whole discount, whatever was billed before."""
from datetime import date
from decimal import Decimal

from django.contrib.contenttypes.models import ContentType
from django.test import TestCase

from company.models import Company
from configurator.models import Configuration, ConfigurationStatus, ProductTemplate
from invoices.models import AppliedTax, TaxType
from invoices.services.project_file_invoice_service import ProjectFileInvoiceService
from management.models import ProjectFile
from sales.models import Client, Quote, QuoteStatus


class _FullRemainingBase(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Test Co")
        self.client_obj = Client.objects.create(name="Acme", company=self.company)
        self.product = ProductTemplate.objects.create(name="Door", company=self.company)
        self.project_file = ProjectFile.objects.create(
            name="PF Full", company=self.company, client=self.client_obj,
        )
        self.iva21, _created = TaxType.objects.get_or_create(
            tax_name="IVA", country="ES", defaults={"behavior": "add"},
        )
        # 1000 subtotal, 100 financing discount, taxable 900, IVA 189
        self.quote = Quote.objects.create(
            project_name="Financed Quote",
            company=self.company,
            client=self.client_obj,
            project_file=self.project_file,
            status=QuoteStatus.ACCEPTED,
            subtotal_price=Decimal("1000.00"),
            discount_amount=Decimal("100.00"),
            discounted_subtotal=Decimal("900.00"),
            tax_amount=Decimal("189.00"),
            total_price=Decimal("1089.00"),
            discount_breakdown=[
                {"rule_name": "Financiación 10%", "kind": "financing",
                 "level": "basket", "mode": "net",
                 "percentage": "10.00", "base": "1000.00", "amount": "100.00"},
            ],
        )
        self.configs = [
            self._make_config(Decimal(amount)) for amount in ("333.33", "333.33", "333.34")
        ]

    def _make_config(self, subtotal, **kwargs):
        config = Configuration.objects.create(
            name="Config",
            company=self.company,
            product=self.product,
            client=self.client_obj,
            quote=self.quote,
            project_file=self.project_file,
            status=ConfigurationStatus.COMPLETED,
            subtotal_price=subtotal,
            total_price=(subtotal * Decimal("1.21")).quantize(Decimal("0.01")),
            **kwargs,
        )
        AppliedTax.objects.create(
            content_type=ContentType.objects.get_for_model(Configuration),
            object_id=config.pk,
            tax_type=self.iva21,
            selected_percentage=Decimal("21"),
            base_amount=subtotal,
            calculated_amount=(subtotal * Decimal("0.21")).quantize(Decimal("0.01")),
        )
        return config

    def _summary(self):
        return ProjectFileInvoiceService.get_invoiceable_summary(self.project_file)

    def _create_invoice(self, items_data):
        return ProjectFileInvoiceService.create_invoice_from_project_file(
            project_file=self.project_file,
            company=self.company,
            client=self.client_obj,
            date_issued=date(2026, 10, 1),
            due_date=None,
            items_data=items_data,
        )

    def _invoice_remaining(self, **kwargs):
        return self._create_invoice(
            ProjectFileInvoiceService.build_items_for_remaining(self._summary(), **kwargs)
        )


class FullRemainingInvoiceTest(_FullRemainingBase):
    def test_first_invoice_bills_the_quote_exactly(self):
        invoice = self._invoice_remaining(split_configs=False)

        self.assertEqual(invoice.subtotal_amount.amount, Decimal("1000.00"))
        self.assertEqual(invoice.discount_amount.amount, Decimal("100.00"))
        self.assertEqual(invoice.total_amount.amount, Decimal("1089.00"))

    def test_closing_after_a_partial_invoice_leaves_nothing_pending(self):
        ratio = ProjectFileInvoiceService.compute_invoice_line_ratio(
            self._summary(), "percentage", Decimal("33.33"),
        )
        first = self._create_invoice(
            ProjectFileInvoiceService.build_items_for_ratio(self._summary(), ratio, split_configs=False)
        )

        second = self._invoice_remaining(split_configs=False)

        summary = self._summary()
        self.assertEqual(summary["remaining_subtotal"], Decimal("0.00"))
        self.assertEqual(summary["goods_remaining_net"], Decimal("0.00"))
        self.assertEqual(
            first.subtotal_amount.amount + second.subtotal_amount.amount, Decimal("1000.00"),
        )
        self.assertEqual(
            first.discount_amount.amount + second.discount_amount.amount, Decimal("100.00"),
        )
        self.assertEqual(
            ProjectFileInvoiceService.invoiced_by_source(self.project_file),
            {f"config_{c.pk}": c.subtotal_price.amount for c in self.configs},
        )

    def test_fully_billed_sources_are_left_out(self):
        config = self.configs[0]
        self._create_invoice([{
            "source_id": f"config_{config.pk}",
            "description": "Config",
            "quantity": Decimal("1"),
            "unit_price": Decimal("333.33"),
            "discount_percentage": Decimal("0.00"),
        }])

        items = ProjectFileInvoiceService.build_items_for_remaining(self._summary(), split_configs=False)

        self.assertNotIn(f"config_{config.pk}", [item["source_id"] for item in items])
        self.assertEqual(len(items), 2)

    def test_quantity_kept_only_when_its_unit_price_is_exact(self):
        exact = self._make_config(Decimal("300.00"), quantity=3)
        inexact = self._make_config(Decimal("100.00"), quantity=3)

        items = {
            item["source_id"]: item
            for item in ProjectFileInvoiceService.build_items_for_remaining(
                self._summary(), split_configs=False,
            )
        }

        self.assertEqual(items[f"config_{exact.pk}"]["quantity"], Decimal("3"))
        self.assertEqual(items[f"config_{exact.pk}"]["unit_price"], Decimal("100.00"))
        self.assertEqual(items[f"config_{inexact.pk}"]["quantity"], Decimal("1"))
        self.assertEqual(items[f"config_{inexact.pk}"]["unit_price"], Decimal("100.00"))

    def test_component_lines_add_up_to_the_configuration(self):
        config = self.configs[2]
        # Its pricing lines miss the subtotal (333.34) by 3 cents
        Configuration.objects.filter(pk=config.pk).update(pricing_breakdown={"lines": [
            {"label": "Perfil", "amount": "200.004"},
            {"label": "Vidrio", "amount": "133.333"},
        ]})

        items = ProjectFileInvoiceService.build_items_for_remaining(self._summary())

        lines = [item for item in items if item["source_id"] == f"config_{config.pk}"]
        self.assertEqual(len(lines), 2)
        self.assertEqual(sum(line["unit_price"] for line in lines), Decimal("333.34"))


class ClosingInvoiceDiscountTest(_FullRemainingBase):
    """The invoice that bills the rest of a quote takes the rest of its
    discount, so the quote's discount adds up exactly however it was split."""

    def _invoice_config(self, config):
        return self._create_invoice([{
            "source_id": f"config_{config.pk}",
            "description": "Config",
            "quantity": Decimal("1"),
            "unit_price": config.subtotal_price.amount,
            "discount_percentage": Decimal("0.00"),
        }])

    def test_each_invoice_tags_its_discount_with_its_quote(self):
        invoice = self._invoice_config(self.configs[0])

        self.assertEqual(
            [entry["quote_id"] for entry in invoice.discount_breakdown], [self.quote.pk],
        )

    def test_the_closing_invoice_takes_the_rounding_of_the_others(self):
        # 100 × 333.33 / 1000 rounds to 33.33 three times: 99.99
        first = self._invoice_config(self.configs[0])
        second = self._invoice_config(self.configs[1])
        last = self._invoice_config(self.configs[2])

        self.assertEqual(first.discount_amount.amount, Decimal("33.33"))
        self.assertEqual(second.discount_amount.amount, Decimal("33.33"))
        self.assertEqual(last.discount_amount.amount, Decimal("33.34"))
        self.assertEqual(last.discount_breakdown[0]["amount"], "33.34")
        self.assertEqual(self._summary()["goods_remaining_net"], Decimal("0.00"))

    def test_the_closing_invoice_taxes_its_own_discounted_base(self):
        self._invoice_config(self.configs[0])
        self._invoice_config(self.configs[1])
        last = self._invoice_config(self.configs[2])

        # 333.34 − 33.34 = 300.00 at 21 %
        self.assertEqual(last.total_amount.amount, Decimal("363.00"))

    def test_older_invoices_without_a_quote_on_their_discount_count_too(self):
        first = self._invoice_config(self.configs[0])
        second = self._invoice_config(self.configs[1])
        for invoice in (first, second):
            invoice.discount_breakdown = [
                {key: value for key, value in entry.items() if key != "quote_id"}
                for entry in invoice.discount_breakdown
            ]
            invoice.save(update_fields=["discount_breakdown"])

        last = self._invoice_config(self.configs[2])

        self.assertEqual(last.discount_amount.amount, Decimal("33.34"))

    def test_cancelled_invoices_give_their_discount_back(self):
        from invoices.models import SalesInvoiceStatus

        first = self._invoice_config(self.configs[0])
        first.status = SalesInvoiceStatus.CANCELLED
        first.save(update_fields=["status"])

        invoice = self._invoice_remaining(split_configs=False)

        self.assertEqual(invoice.discount_amount.amount, Decimal("100.00"))
