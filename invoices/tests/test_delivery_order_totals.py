"""
Tests for taxes + discounts on Delivery Orders.

New-style delivery orders are self-contained documents: gross lines, a header
discount carried from the source quotes via the invoice discount strategies
(financing rules as their own negative line), and their own AppliedTax rows —
so the printed albarán shows lines + discount + taxes + total, and invoicing
a DO is a verbatim copy. Legacy DOs (net-priced config lines, no DO-level
data) keep the old behavior.
"""
from datetime import date
from decimal import Decimal

from django.contrib.contenttypes.models import ContentType
from django.test import TestCase

from company.models import Company
from configurator.models import Configuration, ConfigurationStatus, ProductTemplate
from configurator.services.export.delivery_order import DeliveryOrderExportStrategy
from inventory.models import InventoryItem, ItemIdentityCode
from invoices.models import AppliedTax, TaxType
from invoices.services.delivery_order_service import DeliveryOrderService
from management.models import ProjectFile
from sales.models import (
    Client,
    DeliveryOrder,
    DeliveryOrderItem,
    DeliveryOrderStatus,
    Quote,
    QuoteStatus,
)


class _DeliveryOrderTestBase(TestCase):

    def setUp(self):
        self.company = Company.objects.create(name="Test Co")
        self.client_obj = Client.objects.create(name="Acme", company=self.company)
        self.product = ProductTemplate.objects.create(name="Door", company=self.company)
        self.project_file = ProjectFile.objects.create(
            name="PF DO Totals",
            company=self.company,
            client=self.client_obj,
        )
        self.iva21, _ = TaxType.objects.get_or_create(
            tax_name="IVA", country="ES", defaults={"behavior": "add"}
        )
        self.config_ct = ContentType.objects.get_for_model(Configuration)
        self.quote_ct = ContentType.objects.get_for_model(Quote)

    def _make_quote(self, **kwargs):
        defaults = dict(
            project_name="Quote",
            company=self.company,
            client=self.client_obj,
            project_file=self.project_file,
            status=QuoteStatus.ACCEPTED,
        )
        defaults.update(kwargs)
        return Quote.objects.create(**defaults)

    def _make_config(self, quote, subtotal, total, with_tax=True):
        config = Configuration.objects.create(
            name="Config",
            company=self.company,
            product=self.product,
            client=self.client_obj,
            quote=quote,
            project_file=self.project_file,
            status=ConfigurationStatus.COMPLETED,
            subtotal_price=subtotal,
            total_price=total,
        )
        if with_tax:
            AppliedTax.objects.create(
                content_type=self.config_ct,
                object_id=config.pk,
                tax_type=self.iva21,
                selected_percentage=Decimal("21"),
                base_amount=subtotal,
                calculated_amount=(subtotal * Decimal("0.21")).quantize(Decimal("0.01")),
            )
        return config

    def _config_item_data(self, config, unit_price, quantity=Decimal("1")):
        return {
            "source_content_type_id": self.config_ct.id,
            "source_object_id": config.pk,
            "description": config.name,
            "quantity": quantity,
            "unit_price": unit_price,
            "discount_percentage": Decimal("0.00"),
        }

    def _create_do(self, items_data):
        return DeliveryOrderService.create_delivery_order(
            project_file=self.project_file,
            company=self.company,
            client=self.client_obj,
            items_data=items_data,
        )


class RecalculateTotalsTests(_DeliveryOrderTestBase):
    """Unit math of DeliveryOrder.recalculate_totals."""

    def setUp(self):
        super().setUp()
        self.irpf, _ = TaxType.objects.get_or_create(
            tax_name="IRPF", country="ES", defaults={"behavior": "subtract"}
        )
        self.do = DeliveryOrder.objects.create(
            company=self.company,
            client=self.client_obj,
            project_file=self.project_file,
            status=DeliveryOrderStatus.DRAFT,
        )
        DeliveryOrderItem.objects.create(
            delivery_order=self.do,
            description="Line",
            quantity=Decimal("1"),
            unit_price=Decimal("1000.00"),
        )
        self.do.discount_amount = Decimal("100.00")
        self.do.save(update_fields=["discount_amount", "discount_amount_currency"])
        do_ct = ContentType.objects.get_for_model(DeliveryOrder)
        # 21% IVA and 15% IRPF retention on the discounted base (900)
        AppliedTax.objects.create(
            content_type=do_ct, object_id=self.do.pk,
            tax_type=self.iva21, selected_percentage=Decimal("21"),
            base_amount=Decimal("900.00"), order=0,
        )
        AppliedTax.objects.create(
            content_type=do_ct, object_id=self.do.pk,
            tax_type=self.irpf, selected_percentage=Decimal("15"),
            base_amount=Decimal("900.00"), order=1,
        )

    def test_totals_with_add_and_subtract_taxes(self):
        self.do.recalculate_totals()
        self.do.refresh_from_db()
        self.assertEqual(self.do.subtotal_amount.amount, Decimal("1000.00"))
        # tax = +189 (IVA) - 135 (IRPF) = 54; total = 900 + 189 - 135
        self.assertEqual(self.do.tax_amount.amount, Decimal("54.00"))
        self.assertEqual(self.do.total_amount.amount, Decimal("954.00"))

    def test_recalculate_subtotal_alias_still_works(self):
        result = self.do.recalculate_subtotal()
        self.assertEqual(result.amount, Decimal("1000.00"))
        self.do.refresh_from_db()
        self.assertEqual(self.do.total_amount.amount, Decimal("954.00"))


class CreateDeliveryOrderWithBasketDiscountTests(_DeliveryOrderTestBase):

    def setUp(self):
        super().setUp()
        # 1000 subtotal, 100 basket discount, taxable 900, IVA 189, total 1089
        self.quote = self._make_quote(
            subtotal_price=Decimal("1000.00"),
            discount_amount=Decimal("100.00"),
            discounted_subtotal=Decimal("900.00"),
            tax_amount=Decimal("189.00"),
            total_price=Decimal("1089.00"),
            discount_breakdown=[
                {"rule_name": "10%", "kind": "standard", "level": "basket",
                 "mode": "net", "percentage": "10.00",
                 "base": "1000.00", "amount": "100.00"},
            ],
        )
        self.config = self._make_config(
            self.quote, Decimal("1000.00"), Decimal("1210.00")
        )

    def test_deliverable_items_use_gross_prices(self):
        items = DeliveryOrderService.get_deliverable_items(self.project_file)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["unit_price"], Decimal("1000.00"))

    def test_do_carries_discount_and_taxes(self):
        do = self._create_do([self._config_item_data(self.config, Decimal("1000.00"))])
        self.assertEqual(do.subtotal_amount.amount, Decimal("1000.00"))
        self.assertEqual(do.discount_amount.amount, Decimal("100.00"))
        self.assertEqual(len(do.discount_breakdown), 1)
        self.assertEqual(do.tax_amount.amount, Decimal("189.00"))
        self.assertEqual(do.total_amount.amount, Decimal("1089.00"))
        applied = do.applied_taxes.get()
        self.assertEqual(applied.base_amount.amount, Decimal("900.00"))
        self.assertEqual(applied.calculated_amount.amount, Decimal("189.00"))

    def test_partial_delivery_scales_discount(self):
        do = self._create_do([self._config_item_data(self.config, Decimal("500.00"))])
        self.assertEqual(do.subtotal_amount.amount, Decimal("500.00"))
        self.assertEqual(do.discount_amount.amount, Decimal("50.00"))
        applied = do.applied_taxes.get()
        self.assertEqual(applied.base_amount.amount, Decimal("450.00"))
        self.assertEqual(do.total_amount.amount, Decimal("544.50"))


class FinancingDiscountOnDeliveryOrderTests(_DeliveryOrderTestBase):

    def setUp(self):
        super().setUp()
        self.quote = self._make_quote(
            subtotal_price=Decimal("1000.00"),
            discount_amount=Decimal("100.00"),
            discounted_subtotal=Decimal("900.00"),
            tax_amount=Decimal("189.00"),
            total_price=Decimal("1089.00"),
            discount_breakdown=[
                {"rule_name": "Financiación 10%", "kind": "financing",
                 "level": "basket", "mode": "net", "percentage": "10.00",
                 "base": "1000.00", "amount": "100.00"},
            ],
        )
        self.config = self._make_config(
            self.quote, Decimal("1000.00"), Decimal("1210.00")
        )

    def test_financing_becomes_negative_quote_sourced_line(self):
        do = self._create_do([self._config_item_data(self.config, Decimal("1000.00"))])
        lines = list(do.items.all())
        self.assertEqual(len(lines), 2)
        fin_line = do.items.get(source_content_type=self.quote_ct)
        self.assertEqual(fin_line.source_object_id, self.quote.pk)
        self.assertEqual(fin_line.line_total.amount, Decimal("-100.00"))
        # Not in the header discount — it reduces the subtotal via the line
        self.assertEqual(do.discount_amount.amount, Decimal("0.00"))
        self.assertEqual(do.subtotal_amount.amount, Decimal("900.00"))
        self.assertEqual(do.tax_amount.amount, Decimal("189.00"))
        self.assertEqual(do.total_amount.amount, Decimal("1089.00"))
        applied = do.applied_taxes.get()
        self.assertEqual(applied.base_amount.amount, Decimal("900.00"))

    def test_financing_line_does_not_affect_delivered_quantities(self):
        self._create_do([self._config_item_data(self.config, Decimal("1000.00"))])
        items = DeliveryOrderService.get_deliverable_items(self.project_file)
        self.assertEqual(items, [])  # config fully delivered, no phantom rows


class InvoiceFromDeliveryOrderTests(_DeliveryOrderTestBase):
    """Invoicing a self-contained DO copies its lines/discount/taxes verbatim."""

    def setUp(self):
        super().setUp()
        self.quote = self._make_quote(
            subtotal_price=Decimal("1000.00"),
            discount_amount=Decimal("150.00"),
            discounted_subtotal=Decimal("850.00"),
            tax_amount=Decimal("178.50"),
            total_price=Decimal("1028.50"),
            discount_breakdown=[
                {"rule_name": "10%", "kind": "standard", "level": "basket",
                 "mode": "net", "percentage": "10.00",
                 "base": "1000.00", "amount": "100.00"},
                {"rule_name": "Financiación 5%", "kind": "financing",
                 "level": "basket", "mode": "net", "percentage": "5.00",
                 "base": "1000.00", "amount": "50.00"},
            ],
        )
        self.config = self._make_config(
            self.quote, Decimal("1000.00"), Decimal("1210.00")
        )
        self.do = self._create_do(
            [self._config_item_data(self.config, Decimal("1000.00"))]
        )

    def _invoice(self, delivery_orders):
        return DeliveryOrderService.create_invoice_from_delivery_orders(
            delivery_orders,
            company=self.company,
            client=self.client_obj,
            date_issued=date(2026, 8, 1),
        )

    def test_invoice_matches_do_totals(self):
        invoice = self._invoice([self.do])
        self.assertEqual(invoice.subtotal_amount.amount, self.do.subtotal_amount.amount)
        self.assertEqual(invoice.discount_amount.amount, Decimal("100.00"))
        self.assertEqual(invoice.tax_amount.amount, self.do.tax_amount.amount)
        self.assertEqual(invoice.total_amount.amount, self.do.total_amount.amount)
        self.assertEqual(invoice.total_amount.amount, Decimal("1028.50"))

    def test_financing_line_copied_exactly_once(self):
        invoice = self._invoice([self.do])
        fin_lines = invoice.items.filter(source_id=f"discount_quote_{self.quote.pk}")
        self.assertEqual(fin_lines.count(), 1)
        self.assertEqual(fin_lines.get().line_total.amount, Decimal("-50.00"))

    def test_invoice_tax_base_is_discounted(self):
        invoice = self._invoice([self.do])
        ct = ContentType.objects.get_for_model(invoice)
        applied = AppliedTax.objects.get(content_type=ct, object_id=invoice.pk)
        self.assertEqual(applied.base_amount.amount, Decimal("850.00"))


class LegacyDeliveryOrderInvoiceTests(_DeliveryOrderTestBase):
    """Old DOs (net-priced lines, no DO-level tax/discount) keep the current
    behavior: taxes from source objects on the line totals, no discount."""

    def setUp(self):
        super().setUp()
        self.quote = self._make_quote(
            subtotal_price=Decimal("1000.00"),
            discount_amount=Decimal("100.00"),
            discounted_subtotal=Decimal("900.00"),
            tax_amount=Decimal("189.00"),
            total_price=Decimal("1089.00"),
        )
        self.config = self._make_config(
            self.quote, Decimal("1000.00"), Decimal("1210.00")
        )
        # Hand-built legacy DO: net-priced line, no AppliedTax on the DO
        self.do = DeliveryOrder.objects.create(
            company=self.company,
            client=self.client_obj,
            project_file=self.project_file,
            status=DeliveryOrderStatus.DRAFT,
            subtotal_amount=Decimal("900.00"),
        )
        DeliveryOrderItem.objects.create(
            delivery_order=self.do,
            description="Config (net)",
            quantity=Decimal("1"),
            unit_price=Decimal("900.00"),
            source_content_type=self.config_ct,
            source_object_id=self.config.pk,
        )

    def test_legacy_do_keeps_source_tax_path(self):
        invoice = DeliveryOrderService.create_invoice_from_delivery_orders(
            [self.do],
            company=self.company,
            client=self.client_obj,
            date_issued=date(2026, 8, 1),
        )
        self.assertEqual(invoice.subtotal_amount.amount, Decimal("900.00"))
        self.assertEqual(invoice.discount_amount.amount, Decimal("0.00"))
        self.assertEqual(invoice.tax_amount.amount, Decimal("189.00"))
        self.assertEqual(invoice.total_amount.amount, Decimal("1089.00"))


class DeliveryOrderExportContextTests(_DeliveryOrderTestBase):

    def _financed_do(self):
        quote = self._make_quote(
            subtotal_price=Decimal("1000.00"),
            discount_amount=Decimal("150.00"),
            discounted_subtotal=Decimal("850.00"),
            tax_amount=Decimal("178.50"),
            total_price=Decimal("1028.50"),
            discount_breakdown=[
                {"rule_name": "10%", "kind": "standard", "level": "basket",
                 "mode": "net", "percentage": "10.00",
                 "base": "1000.00", "amount": "100.00"},
                {"rule_name": "Financiación 5%", "kind": "financing",
                 "level": "basket", "mode": "net", "percentage": "5.00",
                 "base": "1000.00", "amount": "50.00"},
            ],
        )
        config = self._make_config(quote, Decimal("1000.00"), Decimal("1210.00"))
        return self._create_do([self._config_item_data(config, Decimal("1000.00"))])

    def test_context_exposes_discount_taxes_and_total(self):
        do = self._financed_do()
        context = DeliveryOrderExportStrategy(do).get_context()
        self.assertEqual(context["subtotal"], 950.0)
        self.assertEqual(context["discount_amount"], 100.0)
        self.assertEqual(context["tax_amount"], 178.5)
        self.assertEqual(context["total"], 1028.5)
        self.assertTrue(context["has_financing_discount"])
        self.assertEqual(context["financing_discount_amount"], 50.0)
        self.assertEqual(len(context["taxes"]), 1)
        self.assertEqual(context["taxes"][0]["base"], 850.0)
        discount_lines = [i for i in context["items"] if i["is_discount_line"]]
        self.assertEqual(len(discount_lines), 1)
        self.assertEqual(discount_lines[0]["line_total"], -50.0)

    def test_context_exposes_identity_codes_per_line(self):
        do = self._financed_do()
        inventory_item = InventoryItem.objects.create(company=self.company, name="Handle")
        line = do.items.exclude(source_content_type_id=self.quote_ct.id).first()
        for code in ("SN-002", "SN-001"):
            ItemIdentityCode.objects.create(
                item=inventory_item, code=code, delivery_order_item=line
            )

        context = DeliveryOrderExportStrategy(do).get_context()

        self.assertTrue(context["has_identity_codes"])
        by_number = {i["item_number"]: i for i in context["items"]}
        traced = by_number[line.item_number]
        self.assertEqual(traced["identity_codes"], ["SN-001", "SN-002"])
        self.assertTrue(traced["has_identity_codes"])
        untraced = [i for i in context["items"] if i["is_discount_line"]][0]
        self.assertEqual(untraced["identity_codes"], [])
        self.assertFalse(untraced["has_identity_codes"])

    def test_context_without_identity_codes(self):
        do = self._financed_do()
        context = DeliveryOrderExportStrategy(do).get_context()
        self.assertFalse(context["has_identity_codes"])
        self.assertTrue(all(i["identity_codes"] == [] for i in context["items"]))

    def test_legacy_do_falls_back_to_subtotal_total(self):
        do = DeliveryOrder.objects.create(
            company=self.company,
            client=self.client_obj,
            status=DeliveryOrderStatus.DRAFT,
            subtotal_amount=Decimal("500.00"),
        )
        context = DeliveryOrderExportStrategy(do).get_context()
        self.assertEqual(context["total"], 500.0)
        self.assertEqual(context["taxes"], [])
        self.assertFalse(context["has_financing_discount"])
