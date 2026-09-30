"""
Golden test for invoicing a project file (expediente).

Pins, to the cent, what ``ProjectFileInvoiceService.create_invoice_from_project_file``
produces for a realistic expediente, so refactors of the pricing code cannot
move any figure. The expediente mixes every source kind and two VAT rates:

- one accepted quote with a configuration (IVA 21 %), two inventory items
  (IVA 10 % and IVA 21 %) and a material line without taxes, discounted by a
  5 % basket rule followed by a 2 % financing rule on the line items only;
- a direct configuration (IVA 21 %);
- a work entry (IVA 21 %).

Lines are built by the API builder (``build_items_from_summary``), as the
mobile app and bulk-emit do. ``price_lines`` and ``build_items_for_ratio``,
the pure pieces behind it, are pinned against the same fixture.

A second fixture pins retentions (IRPF): the quote, the expediente builder,
the delivery-order builder and the bulk builder all land on
base + IVA − IRPF.

On that same fixture, the advance reconciliation: a 30 % advance and the
invoices that deduct it add up, to the cent, to one invoice for everything.
"""
from datetime import date
from decimal import Decimal

from django.contrib.contenttypes.models import ContentType
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from djmoney.money import Money

from company.models import Company
from configurator.models import Configuration, ConfigurationStatus, ProductTemplate
from inventory.models import InventoryItem
from invoices.models import AppliedTax, SalesInvoice, TaxType
from invoices.services.project_file_invoice_service import ProjectFileInvoiceService
from management.models import ProjectFile, WorkEntry, WorkEntryType
from materials.models import Category, Material
from sales.models import (
    Client,
    DiscountKind,
    DiscountLevel,
    DiscountMode,
    DiscountPolicy,
    DiscountRule,
    Quote,
    QuoteInventoryItem,
    QuoteItem,
    QuoteStatus,
)


def _invoice_snapshot(invoice):
    """Everything a pricing refactor could change, read back from the DB."""
    invoice.refresh_from_db()
    ct = ContentType.objects.get_for_model(invoice)
    return {
        # Sorted: quote inventory lines have no defined order.
        "items": sorted(
            (
                item.source_id,
                item.description,
                item.quantity,
                item.unit_price.amount,
                item.discount_percentage,
                item.line_total.amount,
            )
            for item in invoice.items.all()
        ),
        "subtotal": invoice.subtotal_amount.amount,
        "discount": invoice.discount_amount.amount,
        "discount_breakdown": invoice.discount_breakdown,
        "taxes": [
            (
                tax.tax_type.tax_name,
                tax.selected_percentage,
                tax.base_amount.amount,
                tax.calculated_amount.amount,
                tax.order,
            )
            for tax in AppliedTax.objects.filter(content_type=ct, object_id=invoice.pk)
            .select_related("tax_type").order_by("order", "pk")
        ],
        "tax": invoice.tax_amount.amount,
        "total": invoice.total_amount.amount,
    }


class _GoldenExpedienteBase(TestCase):

    def setUp(self):
        self.company = Company.objects.create(name="Golden Co")
        self.client_obj = Client.objects.create(name="Acme", company=self.company)
        self.product = ProductTemplate.objects.create(name="Door", company=self.company)
        self.project_file = ProjectFile.objects.create(
            name="PF Golden", company=self.company, client=self.client_obj,
        )
        self.iva, _ = TaxType.objects.get_or_create(
            tax_name="IVA", country="ES", defaults={"behavior": "add"}
        )

        policy = DiscountPolicy.objects.create(name="Golden policy", company=self.company)
        DiscountRule.objects.create(
            policy=policy, name="Descuento 5%", order=0, percentage=Decimal("5.00"),
            mode=DiscountMode.NET, level=DiscountLevel.BASKET, kind=DiscountKind.STANDARD,
        )
        DiscountRule.objects.create(
            policy=policy, name="Financiación 2%", order=1, percentage=Decimal("2.00"),
            mode=DiscountMode.ACCUMULATED, level=DiscountLevel.LINE_ITEMS_BASKET,
            kind=DiscountKind.FINANCING,
        )

        self.quote = Quote.objects.create(
            project_name="Golden Quote",
            company=self.company,
            client=self.client_obj,
            project_file=self.project_file,
            status=QuoteStatus.ACCEPTED,
        )
        self.quote.discount_policies.add(policy)

        self.config = self._make_config("Corredera", Decimal("1234.56"), quote=self.quote)
        self.inv_10 = self._make_inv_item("Manilla", Decimal("3"), Decimal("123.45"), Decimal("10"))
        self.inv_21 = self._make_inv_item("Mosquitera", Decimal("2"), Decimal("99.99"), Decimal("21"))
        category = Category.objects.create(name="Perfiles", company=self.company)
        material = Material.objects.create(
            reference_code="MAT-G", name="Perfil", category=category,
            cost_per_unit=Decimal("0"),
        )
        self.quote_item = QuoteItem.objects.create(
            quote=self.quote, material=material,
            quantity=Decimal("1"), unit_price=Decimal("87.16"),
        )
        self.quote.recalc_totals()
        self.quote.refresh_from_db()

        self.direct = self._make_config("Ajuste taller", Decimal("150.00"))

        entry_type = WorkEntryType.objects.create(company=self.company, name="Porte")
        self.entry = WorkEntry.objects.create(
            company=self.company,
            project_file=self.project_file,
            entry_type=entry_type,
            description="Porte Madrid",
            quantity=Decimal("2"),
            unit_price=Money("45.50", "EUR"),
            date=date(2026, 9, 1),
        )
        AppliedTax.objects.create(
            content_type=ContentType.objects.get_for_model(WorkEntry),
            object_id=self.entry.pk,
            tax_type=self.iva,
            selected_percentage=Decimal("21"),
            base_amount=Decimal("91.00"),
            calculated_amount=Decimal("19.11"),
        )

    def _make_config(self, name, subtotal, quote=None):
        """A configuration taxed at IVA 21 %; direct (quote-less) if no quote."""
        tax = (subtotal * Decimal("0.21")).quantize(Decimal("0.01"))
        config = Configuration.objects.create(
            name=name,
            company=self.company,
            product=self.product,
            client=self.client_obj,
            quote=quote,
            project_file=self.project_file,
            status=ConfigurationStatus.COMPLETED if quote else ConfigurationStatus.SHOP,
            subtotal_price=subtotal,
            total_price=subtotal + tax,
            tax_breakdown=[{
                "tax_name": self.iva.tax_name, "percentage": "21.00",
                "base": str(subtotal), "tax_amount": str(tax),
            }],
        )
        AppliedTax.objects.create(
            content_type=ContentType.objects.get_for_model(Configuration),
            object_id=config.pk,
            tax_type=self.iva,
            selected_percentage=Decimal("21"),
            base_amount=subtotal,
            calculated_amount=tax,
        )
        return config

    def _make_inv_item(self, name, quantity, unit_price, percentage):
        """A quote inventory line; it copies its stock item's IVA on creation."""
        stock_item = InventoryItem.objects.create(name=name, company=self.company)
        AppliedTax.objects.create(
            content_type=ContentType.objects.get_for_model(InventoryItem),
            object_id=stock_item.pk,
            tax_type=self.iva,
            selected_percentage=percentage,
        )
        return QuoteInventoryItem.objects.create(
            quote=self.quote, inventory_item=stock_item,
            quantity=quantity, unit_price=unit_price,
        )

    def _summary(self):
        return ProjectFileInvoiceService.get_invoiceable_summary(self.project_file)

    def _items(self, percentage):
        from api.billing.serializers import build_items_from_summary

        return build_items_from_summary(self._summary(), "percentage", percentage, None)

    def _create_invoice(self, items_data):
        return ProjectFileInvoiceService.create_invoice_from_project_file(
            project_file=self.project_file,
            company=self.company,
            client=self.client_obj,
            date_issued=date(2026, 9, 29),
            due_date=date(2026, 10, 29),
            items_data=items_data,
        )


FULL_BREAKDOWN = [
    {"amount": "13.15", "base": "657.49", "kind": "financing",
     "level": "line_items_basket", "mode": "accumulated",
     "percentage": "2.00", "rule_name": "Financiación 2%"},
    {"amount": "94.60", "base": "1892.05", "kind": "standard",
     "level": "basket", "mode": "net",
     "percentage": "5.00", "rule_name": "Descuento 5%"},
]


class GoldenFixtureTests(_GoldenExpedienteBase):
    """The fixture itself, so a failure below points at pricing, not setup."""

    def test_quote_totals(self):
        self.assertEqual(self.quote.subtotal_price.amount, Decimal("1892.05"))
        self.assertEqual(self.quote.discount_amount.amount, Decimal("107.75"))
        self.assertEqual(self.quote.discounted_subtotal.amount, Decimal("1784.30"))
        self.assertEqual(self.quote.tax_amount.amount, Decimal("319.03"))
        self.assertEqual(self.quote.total_price.amount, Decimal("2103.33"))
        self.assertEqual(self.quote.discount_breakdown, FULL_BREAKDOWN)

    def test_summary_totals(self):
        summary = self._summary()
        # quote 2103.33 + direct 181.50 + work entry 110.11
        self.assertEqual(summary["total_invoiceable"], Decimal("2394.94"))
        self.assertEqual(summary["total_invoiceable_subtotal"], Decimal("2133.05"))
        self.assertEqual(summary["total_invoiceable_subtotal_net"], Decimal("2025.30"))


class GoldenFullInvoiceTests(_GoldenExpedienteBase):
    """100 % of the expediente in one invoice."""

    def test_invoice_matches_golden_figures(self):
        invoice = self._create_invoice(self._items(Decimal("100")))
        quote_id = self.quote.pk
        self.assertEqual(_invoice_snapshot(invoice), {
            "items": sorted([
                (f"config_{self.config.pk}", "Corredera",
                 Decimal("1.00"), Decimal("1234.56"), Decimal("0.00"), Decimal("1234.56")),
                (f"quote_item_{self.quote_item.pk}", f"[MAT-G] Perfil x1.000 (P#{quote_id})",
                 Decimal("1.00"), Decimal("87.16"), Decimal("0.00"), Decimal("87.16")),
                (f"inv_item_{self.inv_10.pk}", "Manilla",
                 Decimal("3.00"), Decimal("123.45"), Decimal("0.00"), Decimal("370.35")),
                (f"inv_item_{self.inv_21.pk}", "Mosquitera",
                 Decimal("2.00"), Decimal("99.99"), Decimal("0.00"), Decimal("199.98")),
                (f"direct_{self.direct.pk}", "Ajuste taller",
                 Decimal("1.00"), Decimal("150.00"), Decimal("0.00"), Decimal("150.00")),
                (f"work_entry_{self.entry.pk}", "Porte Madrid",
                 Decimal("1.00"), Decimal("91.00"), Decimal("0.00"), Decimal("91.00")),
            ]),
            "subtotal": Decimal("2133.05"),
            "discount": Decimal("107.75"),
            "discount_breakdown": FULL_BREAKDOWN,
            "taxes": [
                # 1172.83 (config) + 185.98 (inv 21 %) + 150.00 (direct) + 91.00 (work entry)
                ("IVA", Decimal("21.00"), Decimal("1599.81"), Decimal("335.96"), 0),
                ("IVA", Decimal("10.00"), Decimal("344.43"), Decimal("34.44"), 1),
            ],
            "tax": Decimal("370.40"),
            "total": Decimal("2395.70"),
        })

    def test_summary_after_full_invoice(self):
        self._create_invoice(self._items(Decimal("100")))
        summary = self._summary()
        # The quote spreads its discount over every VAT base while the invoice
        # keeps the line-items rule on the line items: 0.76 of VAT apart.
        self.assertEqual(summary["remaining"], Decimal("-0.76"))
        self.assertEqual(summary["remaining_subtotal"], Decimal("0.00"))
        self.assertEqual(summary["remaining_subtotal_net"], Decimal("0.00"))


class GoldenPartialInvoiceTests(_GoldenExpedienteBase):
    """40 % of the pending amount: configurations, material lines, direct
    configurations and work entries scale; inventory items go in full."""

    def test_invoice_matches_golden_figures(self):
        invoice = self._create_invoice(self._items(Decimal("40")))
        quote_id = self.quote.pk
        self.assertEqual(_invoice_snapshot(invoice), {
            "items": sorted([
                (f"config_{self.config.pk}", "Corredera",
                 Decimal("1.00"), Decimal("493.82"), Decimal("0.00"), Decimal("493.82")),
                (f"quote_item_{self.quote_item.pk}", f"[MAT-G] Perfil x1.000 (P#{quote_id})",
                 Decimal("1.00"), Decimal("34.86"), Decimal("0.00"), Decimal("34.86")),
                (f"inv_item_{self.inv_10.pk}", "Manilla",
                 Decimal("3.00"), Decimal("123.45"), Decimal("0.00"), Decimal("370.35")),
                (f"inv_item_{self.inv_21.pk}", "Mosquitera",
                 Decimal("2.00"), Decimal("99.99"), Decimal("0.00"), Decimal("199.98")),
                (f"direct_{self.direct.pk}", "Ajuste taller",
                 Decimal("1.00"), Decimal("60.00"), Decimal("0.00"), Decimal("60.00")),
                (f"work_entry_{self.entry.pk}", "Porte Madrid",
                 Decimal("1.00"), Decimal("36.40"), Decimal("0.00"), Decimal("36.40")),
            ]),
            "subtotal": Decimal("1195.41"),
            "discount": Decimal("67.05"),
            "discount_breakdown": [
                {"amount": "12.10", "base": "605.19", "kind": "financing",
                 "level": "line_items_basket", "mode": "accumulated",
                 "percentage": "2.00", "rule_name": "Financiación 2%"},
                {"amount": "54.95", "base": "1099.01", "kind": "standard",
                 "level": "basket", "mode": "net",
                 "percentage": "5.00", "rule_name": "Descuento 5%"},
            ],
            "taxes": [
                ("IVA", Decimal("21.00"), Decimal("751.51"), Decimal("157.82"), 0),
                ("IVA", Decimal("10.00"), Decimal("344.43"), Decimal("34.44"), 1),
            ],
            "tax": Decimal("192.26"),
            "total": Decimal("1320.62"),
        })

    def test_summary_after_partial_invoice(self):
        self._create_invoice(self._items(Decimal("40")))
        summary = self._summary()
        self.assertEqual(summary["remaining"], Decimal("1074.32"))
        self.assertEqual(summary["remaining_subtotal"], Decimal("937.64"))
        self.assertEqual(summary["remaining_subtotal_net"], Decimal("896.94"))


class GoldenPriceLinesTests(_GoldenExpedienteBase):
    """price_lines() prices exactly what create_invoice_from_project_file
    stores, without writing anything."""

    def test_writes_nothing(self):
        summary = self._summary()
        items = self._items(Decimal("100"))
        with CaptureQueriesContext(connection) as ctx:
            ProjectFileInvoiceService.price_lines(summary, items)
        writes = [
            q["sql"] for q in ctx.captured_queries
            if q["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))
        ]
        self.assertEqual(writes, [])
        self.assertFalse(SalesInvoice.objects.filter(project_file=self.project_file).exists())

    def test_matches_the_created_invoice(self):
        items = self._items(Decimal("40"))
        priced = ProjectFileInvoiceService.price_lines(self._summary(), items)
        snapshot = _invoice_snapshot(self._create_invoice(items))

        self.assertEqual(priced.subtotal, snapshot["subtotal"])
        self.assertEqual(priced.resolution.discount_amount, snapshot["discount"])
        self.assertEqual(priced.resolution.breakdown, snapshot["discount_breakdown"])
        self.assertEqual(
            [
                (e["tax_type"].tax_name, e["selected_percentage"], e["base_amount"],
                 e["calculated_amount"], order)
                for order, e in enumerate(priced.tax_agg.values())
            ],
            snapshot["taxes"],
        )
        self.assertEqual(priced.tax_amount, snapshot["tax"])
        self.assertEqual(priced.total_amount, snapshot["total"])

    def test_groups_hold_the_taxable_base_per_vat_group(self):
        priced = ProjectFileInvoiceService.price_lines(self._summary(), self._items(Decimal("100")))
        self.assertEqual(priced.groups, {
            frozenset({(self.iva, Decimal("21"))}): Decimal("1599.81"),
            frozenset({(self.iva, Decimal("10"))}): Decimal("344.43"),
            # the untaxed material line, net of its quote's discount
            frozenset(): Decimal("81.06"),
        })
        # The groups add up to the invoice's taxable base.
        self.assertEqual(
            sum(priced.groups.values()),
            priced.subtotal - priced.resolution.discount_amount,
        )


class BuildItemsForRatioTests(_GoldenExpedienteBase):
    """The service builder behind the API's build_items_from_summary."""

    def test_api_wrapper_uses_the_ratio_builder(self):
        from api.billing.serializers import build_items_from_summary

        summary = self._summary()
        self.assertCountEqual(
            ProjectFileInvoiceService.build_items_for_ratio(summary, Decimal("0.4")),
            build_items_from_summary(summary, "percentage", Decimal("40"), None),
        )

    def test_full_ratio_bills_every_source_in_full(self):
        items = ProjectFileInvoiceService.build_items_for_ratio(self._summary(), Decimal("1"))
        self.assertCountEqual(
            [(i["source_id"], i["unit_price"]) for i in items],
            [
                (f"config_{self.config.pk}", Decimal("1234.56")),
                (f"quote_item_{self.quote_item.pk}", Decimal("87.16")),
                (f"inv_item_{self.inv_10.pk}", Decimal("123.45")),
                (f"inv_item_{self.inv_21.pk}", Decimal("99.99")),
                (f"direct_{self.direct.pk}", Decimal("150.00")),
                (f"work_entry_{self.entry.pk}", Decimal("91.00")),
            ],
        )

    def test_hidden_vehicle_name(self):
        items = ProjectFileInvoiceService.build_items_for_ratio(
            self._summary(), Decimal("1"), show_vehicle_name=False,
        )
        self.assertEqual(
            {i["quote_name"] for i in items if "quote_name" in i}, {"."}
        )

    def test_no_ratio_no_lines(self):
        self.assertEqual(
            ProjectFileInvoiceService.build_items_for_ratio(self._summary(), Decimal("0")), []
        )


class _RetentionExpedienteBase(TestCase):
    """10,000.00 of goods, a 2 % financing discount, IVA 21 % and an IRPF 15 %
    retention: taxable base 9,800.00 + IVA 2,058.00 − IRPF 1,470.00 = 10,388.00.
    """

    def setUp(self):
        self.company = Company.objects.create(name="Retention Co")
        self.client_obj = Client.objects.create(name="Autónomo", company=self.company)
        self.product = ProductTemplate.objects.create(name="Door", company=self.company)
        self.project_file = ProjectFile.objects.create(
            name="PF IRPF", company=self.company, client=self.client_obj,
        )
        self.iva, _ = TaxType.objects.get_or_create(
            tax_name="IVA", country="ES", defaults={"behavior": "add"}
        )
        self.irpf, _ = TaxType.objects.get_or_create(
            tax_name="IRPF", country="ES", defaults={"behavior": "subtract"}
        )

        policy = DiscountPolicy.objects.create(name="Financing", company=self.company)
        DiscountRule.objects.create(
            policy=policy, name="Financiación 2%", percentage=Decimal("2.00"),
            mode=DiscountMode.ACCUMULATED, level=DiscountLevel.BASKET,
            kind=DiscountKind.FINANCING,
        )
        self.quote = Quote.objects.create(
            project_name="IRPF Quote",
            company=self.company,
            client=self.client_obj,
            project_file=self.project_file,
            status=QuoteStatus.ACCEPTED,
        )
        self.quote.discount_policies.add(policy)

        self.config = Configuration.objects.create(
            name="Cerramiento",
            company=self.company,
            product=self.product,
            client=self.client_obj,
            quote=self.quote,
            project_file=self.project_file,
            status=ConfigurationStatus.COMPLETED,
            subtotal_price=Decimal("10000.00"),
            total_price=Decimal("10600.00"),
            tax_breakdown=[
                {"tax_name": "IVA", "percentage": "21.00",
                 "base": "10000.00", "tax_amount": "2100.00"},
                {"tax_name": "IRPF", "percentage": "15.00",
                 "base": "10000.00", "tax_amount": "1500.00"},
            ],
        )
        config_ct = ContentType.objects.get_for_model(Configuration)
        for order, (tax_type, pct, calc) in enumerate((
            (self.iva, Decimal("21"), Decimal("2100.00")),
            (self.irpf, Decimal("15"), Decimal("1500.00")),
        )):
            AppliedTax.objects.create(
                content_type=config_ct, object_id=self.config.pk,
                tax_type=tax_type, selected_percentage=pct,
                base_amount=Decimal("10000.00"), calculated_amount=calc,
                order=order,
            )
        self.quote.recalc_totals()
        self.quote.refresh_from_db()

    def _assert_retention_invoice(self, invoice, extra_base=Decimal("0.00")):
        invoice.refresh_from_db()
        ct = ContentType.objects.get_for_model(invoice)
        taxes = {
            tax.tax_type_id: (tax.base_amount.amount, tax.calculated_amount.amount)
            for tax in AppliedTax.objects.filter(content_type=ct, object_id=invoice.pk)
        }
        base = Decimal("9800.00") + extra_base
        iva = (base * Decimal("0.21")).quantize(Decimal("0.01"))
        irpf = (base * Decimal("0.15")).quantize(Decimal("0.01"))
        self.assertEqual(invoice.discount_amount.amount, Decimal("200.00"))
        # Per-tax amounts stay positive; only the total nets the retention.
        self.assertEqual(taxes, {self.iva.pk: (base, iva), self.irpf.pk: (base, irpf)})
        self.assertEqual(invoice.tax_amount.amount, iva - irpf)
        self.assertEqual(invoice.total_amount.amount, base + iva - irpf)

        # The builder's totals are what the model would recalculate.
        invoice.recalculate_totals()
        invoice.refresh_from_db()
        self.assertEqual(invoice.total_amount.amount, base + iva - irpf)

    def _full_items(self):
        from api.billing.serializers import build_items_from_summary

        summary = ProjectFileInvoiceService.get_invoiceable_summary(self.project_file)
        return build_items_from_summary(summary, "percentage", Decimal("100"), None)


class GoldenRetentionTests(_RetentionExpedienteBase):
    """Quote, expediente builder, delivery-order builder and bulk builder all
    land on base + IVA − IRPF."""

    def test_quote(self):
        self.assertEqual(self.quote.discounted_subtotal.amount, Decimal("9800.00"))
        self.assertEqual(self.quote.tax_amount.amount, Decimal("588.00"))
        self.assertEqual(self.quote.total_price.amount, Decimal("10388.00"))
        irpf_entry = next(e for e in self.quote.tax_breakdown if e["tax_name"] == "IRPF")
        self.assertEqual(irpf_entry["tax_amount"], "1470.00")

    def test_expediente_builder(self):
        invoice = ProjectFileInvoiceService.create_invoice_from_project_file(
            project_file=self.project_file,
            company=self.company,
            client=self.client_obj,
            date_issued=date(2026, 9, 29),
            due_date=date(2026, 10, 29),
            items_data=self._full_items(),
        )
        self._assert_retention_invoice(invoice)
        summary = ProjectFileInvoiceService.get_invoiceable_summary(self.project_file)
        self.assertEqual(summary["total_invoiceable"], Decimal("10388.00"))
        self.assertEqual(summary["remaining"], Decimal("0.00"))

    def test_delivery_order_builder(self):
        from invoices.services.delivery_order_service import DeliveryOrderService

        items_data = [
            {**item, "quantity": item["remaining_quantity"]}
            for item in DeliveryOrderService.get_deliverable_items(self.project_file)
        ]
        delivery_order = DeliveryOrderService.create_delivery_order(
            project_file=self.project_file,
            company=self.company,
            client=self.client_obj,
            items_data=items_data,
        )
        self.assertEqual(delivery_order.total_amount.amount, Decimal("10388.00"))

        invoice = DeliveryOrderService.create_invoice_from_delivery_orders(
            [delivery_order],
            company=self.company,
            client=self.client_obj,
            date_issued=date(2026, 9, 29),
        )
        self._assert_retention_invoice(invoice)

    def test_bulk_builder(self):
        from invoices.services.bulk_invoice_service import BulkProjectFileInvoiceService

        invoice = BulkProjectFileInvoiceService.create_bulk_invoice(
            [self.project_file],
            company=self.company,
            client=self.client_obj,
            date_issued=date(2026, 9, 29),
        )
        self._assert_retention_invoice(invoice)

    def test_work_entry_retention_is_subtracted(self):
        """A work entry with IVA + IRPF counts base + IVA − IRPF as invoiceable,
        so invoicing everything leaves nothing pending."""
        entry_type = WorkEntryType.objects.create(company=self.company, name="Montaje")
        entry = WorkEntry.objects.create(
            company=self.company,
            project_file=self.project_file,
            entry_type=entry_type,
            description="Montaje",
            quantity=Decimal("1"),
            unit_price=Money("100.00", "EUR"),
            date=date(2026, 9, 1),
        )
        entry_ct = ContentType.objects.get_for_model(WorkEntry)
        for order, (tax_type, pct, calc) in enumerate((
            (self.iva, Decimal("21"), Decimal("21.00")),
            (self.irpf, Decimal("15"), Decimal("15.00")),
        )):
            AppliedTax.objects.create(
                content_type=entry_ct, object_id=entry.pk,
                tax_type=tax_type, selected_percentage=pct,
                base_amount=Decimal("100.00"), calculated_amount=calc, order=order,
            )
        self.assertEqual(entry.total_with_tax, Money("106.00", "EUR"))

        invoice = ProjectFileInvoiceService.create_invoice_from_project_file(
            project_file=self.project_file,
            company=self.company,
            client=self.client_obj,
            date_issued=date(2026, 9, 29),
            due_date=date(2026, 10, 29),
            items_data=self._full_items(),
        )
        self._assert_retention_invoice(invoice, extra_base=Decimal("100.00"))
        summary = ProjectFileInvoiceService.get_invoiceable_summary(self.project_file)
        self.assertEqual(summary["total_invoiceable"], Decimal("10494.00"))
        self.assertEqual(summary["remaining"], Decimal("0.00"))

    def test_retention_joins_its_vat_group(self):
        summary = ProjectFileInvoiceService.get_invoiceable_summary(self.project_file)
        priced = ProjectFileInvoiceService.price_lines(summary, self._full_items())
        self.assertEqual(priced.groups, {
            frozenset({(self.iva, Decimal("21")), (self.irpf, Decimal("15"))}): Decimal("9800.00"),
        })
        self.assertEqual(priced.tax_amount, Decimal("588.00"))
        self.assertEqual(priced.total_amount, Decimal("10388.00"))


@override_settings(AEAT_SUBMISSIONS_ENABLED=False)
class GoldenAdvanceReconciliationTests(_RetentionExpedienteBase):
    """The retention fixture (base 9,800.00, IVA 2,058.00, IRPF 1,470.00,
    total 10,388.00) with a 30 % advance: base 2,940.00 + IVA 617.40 − IRPF
    441.00 = 3,116.40."""

    def setUp(self):
        super().setUp()
        from invoices.services.advances import AdvanceInvoiceService
        from invoices.services.sales_invoice_service import SalesInvoiceService

        self.advance = SalesInvoiceService.finalize_invoice(
            AdvanceInvoiceService.create(
                self.project_file, date_issued=date(2026, 9, 3), percentage=Decimal("30"),
            ),
            send_email=False,
        )

    @staticmethod
    def _figures(invoice):
        invoice.refresh_from_db()
        ct = ContentType.objects.get_for_model(invoice)
        taxes = {
            tax.tax_type.tax_name: (tax.base_amount.amount, tax.calculated_amount.amount)
            for tax in AppliedTax.objects.filter(content_type=ct, object_id=invoice.pk)
            .select_related("tax_type")
        }
        return (
            invoice.subtotal_amount.amount,
            invoice.discount_amount.amount,
            invoice.taxable_base.amount,
            taxes,
            invoice.total_amount.amount,
        )

    def _rest(self):
        from invoices.services.advances import AdvanceDeductionService

        invoice = ProjectFileInvoiceService.create_invoice_from_project_file(
            project_file=self.project_file,
            company=self.company,
            client=self.client_obj,
            date_issued=date(2026, 9, 29),
            due_date=None,
            items_data=self._full_items(),
        )
        AdvanceDeductionService.apply(invoice)
        return invoice

    def _delivery_order_invoice(self):
        """One unit (5,000.00) of the two the configuration now has."""
        from invoices.services.delivery_order_service import DeliveryOrderService

        [item] = DeliveryOrderService.get_deliverable_items(self.project_file)
        delivery_order = DeliveryOrderService.create_delivery_order(
            project_file=self.project_file,
            company=self.company,
            client=self.client_obj,
            items_data=[{**item, "quantity": Decimal("1")}],
        )
        before = self._delivery_order_figures(delivery_order)
        invoice = DeliveryOrderService.create_invoice_from_delivery_orders(
            [delivery_order], company=self.company, client=self.client_obj,
            date_issued=date(2026, 9, 29),
        )
        # The order is left as it is; the invoice copies its discount
        self.assertEqual(self._delivery_order_figures(delivery_order), before)
        self.assertEqual(invoice.discount_breakdown, delivery_order.discount_breakdown)
        return invoice

    @staticmethod
    def _delivery_order_figures(delivery_order):
        delivery_order.refresh_from_db()
        return (
            delivery_order.subtotal_amount.amount,
            delivery_order.discount_amount.amount,
            delivery_order.tax_amount.amount,
            delivery_order.total_amount.amount,
            [
                (tax.tax_type_id, tax.base_amount.amount, tax.calculated_amount.amount)
                for tax in delivery_order.applied_taxes.order_by("pk")
            ],
        )

    def test_advance(self):
        self.assertEqual(self._figures(self.advance), (
            Decimal("2940.00"), Decimal("0.00"), Decimal("2940.00"),
            {"IVA": (Decimal("2940.00"), Decimal("617.40")), "IRPF": (Decimal("2940.00"), Decimal("441.00"))},
            Decimal("3116.40"),
        ))

    def test_advance_and_rest_add_up_to_the_expediente(self):
        from api._serializers import project_file_can_mark_completed
        from invoices.models import InvoicePaymentStatus

        rest = self._rest()

        # 10,000 − 2,940 of lines, the whole 200.00 discount
        self.assertEqual(self._figures(rest), (
            Decimal("7060.00"), Decimal("200.00"), Decimal("6860.00"),
            {"IVA": (Decimal("6860.00"), Decimal("1440.60")), "IRPF": (Decimal("6860.00"), Decimal("1029.00"))},
            Decimal("7271.60"),
        ))
        summary = ProjectFileInvoiceService.get_invoiceable_summary(self.project_file)
        self.assertEqual(
            self.advance.total_amount.amount + rest.total_amount.amount, Decimal("10388.00"),
        )
        self.assertEqual(summary["total_invoiceable"], Decimal("10388.00"))
        self.assertEqual(summary["remaining"], Decimal("0.00"))
        self.assertEqual(summary["advances_open_net"], Decimal("0.00"))

        project_file = ProjectFile.objects.get(pk=self.project_file.pk)
        self.assertFalse(project_file_can_mark_completed(project_file))
        SalesInvoice.objects.filter(pk__in=[self.advance.pk, rest.pk]).update(
            payment_status=InvoicePaymentStatus.PAID,
        )
        self.assertTrue(project_file_can_mark_completed(project_file))

    def test_two_delivery_order_invoices_deduct_half_each(self):
        Configuration.objects.filter(pk=self.config.pk).update(units=Decimal("2"))

        invoices = [self._delivery_order_invoice(), self._delivery_order_invoice()]

        for invoice in invoices:
            with self.subTest(invoice=invoice.pk):
                deduction = invoice.items.get(advance_line__isnull=False)
                self.assertEqual(deduction.line_total.amount, Decimal("-1470.00"))
                # 5,000 − 1,470 of lines, 100.00 of discount: base 3,430.00
                self.assertEqual(self._figures(invoice), (
                    Decimal("3530.00"), Decimal("100.00"), Decimal("3430.00"),
                    {"IVA": (Decimal("3430.00"), Decimal("720.30")), "IRPF": (Decimal("3430.00"), Decimal("514.50"))},
                    Decimal("3635.80"),
                ))
        self.assertEqual(
            self.advance.total_amount.amount + sum(i.total_amount.amount for i in invoices),
            Decimal("10388.00"),
        )
        summary = ProjectFileInvoiceService.get_invoiceable_summary(self.project_file)
        self.assertEqual(summary["goods_remaining_net"], Decimal("0.00"))
        self.assertEqual(summary["advances_open_net"], Decimal("0.00"))

    def test_a_cancelled_advance_leaves_a_full_invoice(self):
        from invoices.models import SalesInvoiceStatus

        self.advance.status = SalesInvoiceStatus.CANCELLED
        self.advance.save(update_fields=["status"])

        rest = self._rest()

        self.assertFalse(rest.items.filter(advance_line__isnull=False).exists())
        self._assert_retention_invoice(rest)
        self.assertEqual(rest.total_amount.amount, Decimal("10388.00"))
        project_file = ProjectFile.objects.get(pk=self.project_file.pk)
        self.assertEqual(project_file.percentage_invoiced, Decimal("100.00"))
        summary = ProjectFileInvoiceService.get_invoiceable_summary(self.project_file)
        self.assertEqual(summary["percentage_invoiced"], Decimal("100.00"))
        self.assertEqual(summary["remaining"], Decimal("0.00"))
