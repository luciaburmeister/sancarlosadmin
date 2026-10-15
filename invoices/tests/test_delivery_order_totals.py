"""
Tests for taxes + discounts on Delivery Orders.

New-style delivery orders are self-contained documents: gross lines, a header
discount carried from the source quotes via the invoice discount strategies
(standard and financing rules alike), and their own AppliedTax rows — so the
printed albarán shows lines + discount + taxes + total, and invoicing a DO is
a verbatim copy. Legacy DOs (net-priced config lines, no DO-level data) keep
the old behavior.
"""
from datetime import date
from decimal import Decimal

from django.contrib.contenttypes.models import ContentType
from django.test import TestCase

from configurator.services.export.delivery_order import DeliveryOrderExportStrategy
from configurator.services.export.sales_invoice import SalesInvoiceExportStrategy
from inventory.models import InventoryItem, ItemIdentityCode
from invoices.models import AppliedTax, SalesInvoiceItem
from invoices.services.delivery_order_service import DeliveryOrderService
from invoices.tests.factories import (
    ExpedienteMixin,
    discounted_quote,
    do_line,
    financed_quote,
    get_irpf,
)
from sales.models import (
    DeliveryOrder,
    DeliveryOrderItem,
    DeliveryOrderStatus,
    Quote,
)


class _DeliveryOrderTestBase(ExpedienteMixin, TestCase):
    project_file_name = "PF DO Totals"

    def setUp(self):
        super().setUp()
        self.quote_ct = ContentType.objects.get_for_model(Quote)


class RecalculateTotalsTests(_DeliveryOrderTestBase):
    """Unit math of DeliveryOrder.recalculate_totals."""

    def setUp(self):
        super().setUp()
        self.irpf = get_irpf()
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
        self.quote = discounted_quote(project_file=self.project_file)
        self.config = self._make_config(self.quote, "1000.00", total=Decimal("1210.00"))

    def test_deliverable_items_use_gross_prices(self):
        items = DeliveryOrderService.get_deliverable_items(self.project_file)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["unit_price"], Decimal("1000.00"))

    def test_do_carries_discount_and_taxes(self):
        do = self._create_do([do_line(self.config, unit_price=Decimal("1000.00"))])
        self.assertEqual(do.subtotal_amount.amount, Decimal("1000.00"))
        self.assertEqual(do.discount_amount.amount, Decimal("100.00"))
        self.assertEqual(len(do.discount_breakdown), 1)
        self.assertEqual(do.tax_amount.amount, Decimal("189.00"))
        self.assertEqual(do.total_amount.amount, Decimal("1089.00"))
        applied = do.applied_taxes.get()
        self.assertEqual(applied.base_amount.amount, Decimal("900.00"))
        self.assertEqual(applied.calculated_amount.amount, Decimal("189.00"))

    def test_partial_delivery_scales_discount(self):
        do = self._create_do([do_line(self.config, unit_price=Decimal("500.00"))])
        self.assertEqual(do.subtotal_amount.amount, Decimal("500.00"))
        self.assertEqual(do.discount_amount.amount, Decimal("50.00"))
        applied = do.applied_taxes.get()
        self.assertEqual(applied.base_amount.amount, Decimal("450.00"))
        self.assertEqual(do.total_amount.amount, Decimal("544.50"))


class ClientTaxModeDeliveryOrderTests(_DeliveryOrderTestBase):
    """With ``auto_apply_client_taxes`` on, the quote applies its header
    taxes to the whole discounted subtotal, so a configuration that never
    got AppliedTax rows of its own is still taxed on the quote. The DO must
    tax that line the same way (production case: a 180 € configuration
    without taxes left the DO and its invoice 37.80 € short).
    """

    def setUp(self):
        super().setUp()
        # 820 taxed config + 180 bare config = 1000; 10% basket discount;
        # taxable 900; quote taxes everything at 21% → 189; total 1089.
        self.quote = discounted_quote(project_file=self.project_file)
        self.taxed_config = self._make_config(
            self.quote, "820.00", total=Decimal("992.20"),
        )
        self.bare_config = self._make_config(
            self.quote, Decimal("180.00"), total=Decimal("180.00"), tax_pct=None,
        )
        self.items = [
            do_line(self.taxed_config, unit_price=Decimal("820.00")),
            do_line(self.bare_config, unit_price=Decimal("180.00")),
        ]

    def _enable_client_tax_mode(self, with_header_taxes=True):
        self.company.auto_apply_client_taxes = True
        self.company.save(update_fields=["auto_apply_client_taxes"])
        if with_header_taxes:
            # What ClientTaxStrategy.sync_taxes leaves on the quote header.
            AppliedTax.objects.create(
                content_type=self.quote_ct,
                object_id=self.quote.pk,
                tax_type=self.iva21,
                selected_percentage=Decimal("21"),
                base_amount=Decimal("900.00"),
                calculated_amount=Decimal("189.00"),
            )

    def test_bare_config_is_taxed_like_on_the_quote(self):
        self._enable_client_tax_mode()
        do = self._create_do(self.items)
        self.assertEqual(do.subtotal_amount.amount, Decimal("1000.00"))
        self.assertEqual(do.discount_amount.amount, Decimal("100.00"))
        self.assertEqual(do.tax_amount.amount, self.quote.tax_amount.amount)
        self.assertEqual(do.total_amount.amount, self.quote.total_price.amount)
        applied = do.applied_taxes.get()
        self.assertEqual(applied.base_amount.amount, Decimal("900.00"))
        self.assertEqual(applied.calculated_amount.amount, Decimal("189.00"))

    def test_invoice_from_do_matches_quote(self):
        self._enable_client_tax_mode()
        do = self._create_do(self.items)
        invoice = DeliveryOrderService.create_invoice_from_delivery_orders(
            [do],
            company=self.company,
            client=self.client_obj,
            date_issued=date(2026, 9, 15),
        )
        self.assertEqual(invoice.tax_amount.amount, Decimal("189.00"))
        self.assertEqual(invoice.total_amount.amount, Decimal("1089.00"))

    def test_source_mode_keeps_per_source_taxes(self):
        # Flag off: the quote's SourceTaxStrategy only taxes sources that
        # carry taxes, and so does the DO. 820 * 0.9 * 0.21 = 154.98.
        do = self._create_do(self.items)
        self.assertEqual(do.tax_amount.amount, Decimal("154.98"))
        self.assertEqual(do.applied_taxes.get().base_amount.amount, Decimal("738.00"))

    def test_client_mode_without_header_rows_falls_back_to_sources(self):
        self._enable_client_tax_mode(with_header_taxes=False)
        do = self._create_do(self.items)
        self.assertEqual(do.tax_amount.amount, Decimal("154.98"))


class FinancingDiscountOnDeliveryOrderTests(_DeliveryOrderTestBase):

    def setUp(self):
        super().setUp()
        self.quote = discounted_quote(project_file=self.project_file, kind="financing")
        self.config = self._make_config(self.quote, "1000.00", total=Decimal("1210.00"))

    def test_financing_goes_to_header_discount(self):
        do = self._create_do([do_line(self.config, unit_price=Decimal("1000.00"))])
        self.assertEqual(do.items.count(), 1)
        self.assertFalse(do.items.filter(source_content_type=self.quote_ct).exists())
        self.assertEqual(do.discount_amount.amount, Decimal("100.00"))
        self.assertEqual(
            [(e["kind"], e["amount"]) for e in do.discount_breakdown],
            [("financing", "100.00")],
        )
        self.assertEqual(do.subtotal_amount.amount, Decimal("1000.00"))
        self.assertEqual(do.tax_amount.amount, Decimal("189.00"))
        self.assertEqual(do.total_amount.amount, Decimal("1089.00"))
        applied = do.applied_taxes.get()
        self.assertEqual(applied.base_amount.amount, Decimal("900.00"))

    def test_financing_does_not_affect_delivered_quantities(self):
        self._create_do([do_line(self.config, unit_price=Decimal("1000.00"))])
        items = DeliveryOrderService.get_deliverable_items(self.project_file)
        self.assertEqual(items, [])  # config fully delivered, no phantom rows


class InvoiceFromDeliveryOrderTests(_DeliveryOrderTestBase):
    """Invoicing a self-contained DO copies its lines, discount and taxes
    verbatim."""

    def setUp(self):
        super().setUp()
        self.quote = financed_quote(project_file=self.project_file)
        self.config = self._make_config(self.quote, "1000.00", total=Decimal("1210.00"))
        self.do = self._create_do(
            [do_line(self.config, unit_price=Decimal("1000.00"))]
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
        self.assertEqual(invoice.discount_amount.amount, self.do.discount_amount.amount)
        self.assertEqual(invoice.tax_amount.amount, self.do.tax_amount.amount)
        self.assertEqual(invoice.total_amount.amount, self.do.total_amount.amount)
        self.assertEqual(invoice.subtotal_amount.amount, Decimal("1000.00"))
        self.assertEqual(invoice.discount_amount.amount, Decimal("150.00"))
        self.assertEqual(invoice.total_amount.amount, Decimal("1028.50"))

    def test_invoice_copies_discount_breakdown(self):
        invoice = self._invoice([self.do])
        self.assertEqual(invoice.items.count(), 1)
        self.assertFalse(
            invoice.items.filter(source_id=f"discount_quote_{self.quote.pk}").exists()
        )
        self.assertEqual(invoice.discount_breakdown, self.do.discount_breakdown)
        self.assertEqual(
            [(e["kind"], e["amount"]) for e in invoice.discount_breakdown],
            [("standard", "100.00"), ("financing", "50.00")],
        )

    def test_invoice_tax_base_is_discounted(self):
        invoice = self._invoice([self.do])
        ct = ContentType.objects.get_for_model(invoice)
        applied = AppliedTax.objects.get(content_type=ct, object_id=invoice.pk)
        self.assertEqual(applied.base_amount.amount, Decimal("850.00"))

    def test_export_context_shows_financing_inside_the_general_discount(self):
        invoice = self._invoice([self.do])
        context = SalesInvoiceExportStrategy(invoice).get_context()

        self.assertFalse(context["has_financing_discount"])
        self.assertEqual(context["financing_discount_amount"], 0.0)
        self.assertEqual(context["lines_total"], 1000.0)
        self.assertEqual(context["subtotal"], 850.0)
        self.assertEqual(context["discount_amount"], 150.0)
        self.assertEqual(context["discount_percentage"], 15.0)
        self.assertEqual(context["total_amount"], 1028.5)
        self.assertEqual(len(context["discount_breakdown"]), 2)

        self.assertFalse(any(i["is_discount_line"] for i in context["items"]))
        self.assertEqual(len(context["items"]), 1)
        self.assertEqual(context["items"][0]["line_total"], 1000.0)

    def test_export_context_still_handles_legacy_financing_lines(self):
        invoice = self._invoice([self.do])
        SalesInvoiceItem.objects.create(
            invoice=invoice,
            description="Financing discount — legacy",
            quantity=Decimal("1"),
            unit_price=Decimal("-50.00"),
            source_id=f"discount_quote_{self.quote.pk}",
        )
        context = SalesInvoiceExportStrategy(invoice).get_context()

        self.assertTrue(context["has_financing_discount"])
        self.assertEqual(context["financing_discount_amount"], 50.0)
        discount_lines = [i for i in context["items"] if i["is_discount_line"]]
        self.assertEqual(len(discount_lines), 1)
        self.assertEqual(discount_lines[0]["quote_id"], self.quote.pk)

    def test_export_context_without_financing(self):
        self.quote.discount_breakdown = [
            {"rule_name": "10%", "kind": "standard", "level": "basket",
             "mode": "net", "percentage": "10.00",
             "base": "1000.00", "amount": "100.00"},
        ]
        self.quote.discount_amount = Decimal("100.00")
        self.quote.save()
        do = self._create_do(
            [do_line(self.config, unit_price=Decimal("1000.00"))]
        )
        context = SalesInvoiceExportStrategy(self._invoice([do])).get_context()

        self.assertFalse(context["has_financing_discount"])
        self.assertEqual(context["financing_discount_amount"], 0.0)
        self.assertEqual(context["discount_percentage"], 10.0)
        self.assertFalse(any(i["is_discount_line"] for i in context["items"]))


class RecomputeAfterLineEditTests(_DeliveryOrderTestBase):
    """Editing the lines re-derives the carried discount and the tax bases."""

    def setUp(self):
        super().setUp()
        self.quote = financed_quote(project_file=self.project_file)
        self.config = self._make_config(self.quote, "1000.00", total=Decimal("1210.00"))
        self.do = self._create_do(
            [do_line(self.config, unit_price=Decimal("1000.00"))]
        )

    def _goods_line(self):
        return self.do.items.get()

    def test_halving_a_line_rescales_discount_and_tax(self):
        line = self._goods_line()
        line.unit_price = Decimal("500.00")
        line.save()

        DeliveryOrderService.recompute_discounts_and_taxes(self.do)
        self.do.refresh_from_db()

        self.assertEqual(self.do.discount_amount.amount, Decimal("75.00"))
        self.assertEqual(
            [e["amount"] for e in self.do.discount_breakdown], ["50.00", "25.00"]
        )
        self.assertEqual(self.do.subtotal_amount.amount, Decimal("500.00"))
        applied = self.do.applied_taxes.get()
        self.assertEqual(applied.base_amount.amount, Decimal("425.00"))
        self.assertEqual(applied.calculated_amount.amount, Decimal("89.25"))
        self.assertEqual(self.do.total_amount.amount, Decimal("514.25"))

    def test_recompute_is_idempotent(self):
        for _ in range(3):
            DeliveryOrderService.recompute_discounts_and_taxes(self.do)
        self.do.refresh_from_db()
        self.assertEqual(self.do.items.count(), 1)
        self.assertEqual(self.do.applied_taxes.count(), 1)
        self.assertEqual(self.do.discount_amount.amount, Decimal("150.00"))
        self.assertEqual(len(self.do.discount_breakdown), 2)
        self.assertEqual(self.do.total_amount.amount, Decimal("1028.50"))

    def test_removing_the_last_goods_line_drops_discount_and_taxes(self):
        self._goods_line().delete()

        DeliveryOrderService.recompute_discounts_and_taxes(self.do)
        self.do.refresh_from_db()

        self.assertFalse(self.do.items.exists())
        self.assertFalse(self.do.applied_taxes.exists())
        self.assertEqual(self.do.discount_amount.amount, Decimal("0.00"))
        self.assertEqual(self.do.total_amount.amount, Decimal("0.00"))

    def test_edited_do_invoices_consistently(self):
        line = self._goods_line()
        line.unit_price = Decimal("500.00")
        line.save()
        DeliveryOrderService.recompute_discounts_and_taxes(self.do)

        invoice = DeliveryOrderService.create_invoice_from_delivery_orders(
            [self.do],
            company=self.company,
            client=self.client_obj,
            date_issued=date(2026, 8, 1),
        )

        self.assertEqual(invoice.subtotal_amount.amount, Decimal("500.00"))
        self.assertEqual(invoice.discount_amount.amount, Decimal("75.00"))
        self.assertEqual(invoice.tax_amount.amount, Decimal("89.25"))
        self.assertEqual(invoice.total_amount.amount, Decimal("514.25"))
        self.assertFalse(
            invoice.items.filter(source_id=f"discount_quote_{self.quote.pk}").exists()
        )
        fin_entry = [e for e in invoice.discount_breakdown if e["kind"] == "financing"]
        self.assertEqual([e["amount"] for e in fin_entry], ["25.00"])

    def test_manual_line_without_a_source_is_kept_and_untaxed(self):
        DeliveryOrderItem.objects.create(
            delivery_order=self.do,
            description="Portes",
            quantity=Decimal("1"),
            unit_price=Decimal("30.00"),
        )

        DeliveryOrderService.recompute_discounts_and_taxes(self.do)
        self.do.refresh_from_db()

        # Carried discount and tax base still follow the quote-sourced line only
        self.assertEqual(self.do.discount_amount.amount, Decimal("150.00"))
        self.assertEqual(self.do.applied_taxes.get().base_amount.amount, Decimal("850.00"))
        self.assertEqual(self.do.subtotal_amount.amount, Decimal("1030.00"))
        self.assertEqual(self.do.total_amount.amount, Decimal("1058.50"))


class LegacyDeliveryOrderInvoiceTests(_DeliveryOrderTestBase):
    """Old DOs (net-priced lines, no DO-level tax/discount) keep the current
    behavior: taxes from source objects on the line totals, no discount."""

    def setUp(self):
        super().setUp()
        self.quote = discounted_quote(
            project_file=self.project_file, discount_breakdown=[],
        )
        self.config = self._make_config(self.quote, "1000.00", total=Decimal("1210.00"))
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
        quote = financed_quote(project_file=self.project_file)
        config = self._make_config(quote, "1000.00", total=Decimal("1210.00"))
        return self._create_do([do_line(config, unit_price=Decimal("1000.00"))])

    def test_context_exposes_discount_taxes_and_total(self):
        do = self._financed_do()
        context = DeliveryOrderExportStrategy(do).get_context()
        # "subtotal" is the taxable base printed on the document: lines
        # total net of the header discount, so subtotal + tax == total.
        self.assertEqual(context["lines_total"], 1000.0)
        self.assertEqual(context["gross_subtotal"], 1000.0)
        self.assertEqual(context["subtotal"], 850.0)
        self.assertEqual(context["discount_amount"], 150.0)
        self.assertEqual(context["subtotal"] + context["tax_amount"], context["total"])
        self.assertEqual(len(context["discount_breakdown"]), 2)
        self.assertEqual(context["tax_amount"], 178.5)
        self.assertEqual(context["total"], 1028.5)
        self.assertFalse(context["has_financing_discount"])
        self.assertEqual(context["financing_discount_amount"], 0.0)
        self.assertEqual(len(context["taxes"]), 1)
        self.assertEqual(context["taxes"][0]["base"], 850.0)
        self.assertFalse(any(i["is_discount_line"] for i in context["items"]))

    def test_context_exposes_identity_codes_per_line(self):
        do = self._financed_do()
        inventory_item = InventoryItem.objects.create(company=self.company, name="Handle")
        line = do.items.get()
        untraced_line = DeliveryOrderItem.objects.create(
            delivery_order=do,
            description="Portes",
            quantity=Decimal("1"),
            unit_price=Decimal("30.00"),
        )
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
        untraced = by_number[untraced_line.item_number]
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
        self.assertEqual(context["subtotal"], 500.0)
        self.assertEqual(context["lines_total"], 500.0)
        self.assertEqual(context["total"], 500.0)
        self.assertEqual(context["taxes"], [])
        self.assertFalse(context["has_financing_discount"])

    def test_taxable_base_is_subtotal_after_discount(self):
        do = self._financed_do()
        self.assertEqual(do.subtotal_amount.amount, Decimal("1000.00"))
        self.assertEqual(do.discount_amount.amount, Decimal("150.00"))
        self.assertEqual(do.taxable_base.amount, Decimal("850.00"))
        self.assertEqual(str(do.taxable_base.currency), "EUR")
        self.assertEqual(
            do.taxable_base.amount + do.tax_amount.amount, do.total_amount.amount
        )
        self.assertEqual(do.recalculate_totals(save=False)["taxable_base"], do.taxable_base)
