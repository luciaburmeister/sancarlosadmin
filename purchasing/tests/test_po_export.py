"""
Purchase orders as an export template kind: the strategy's context, template
resolution through the kind registry, and the admin PDF preview.

Run:
    python manage.py test purchasing.tests.test_po_export
"""
from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from djmoney.money import Money

from company.models import Company
from configurator.models import ConfigurationExportTemplate, ExportTemplateKind
from configurator.services.export import ConfigurationExportService
from configurator.services.export.kind_registry import get_preview_object
from configurator.services.export.purchase_order import PurchaseOrderExportStrategy
from inventory.models import InventoryItem
from invoices.models import AppliedTax, TaxType
from materials.models import Provider
from purchasing.models import PurchaseOrder, PurchaseOrderItem, PurchaseOrderType

User = get_user_model()


class PurchaseOrderExportTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name="Export Co", tax_identifier="B12345678")
        cls.provider = Provider.objects.create(
            company=cls.company,
            name="Perfiles del Sur S.L.",
            contact="Ana",
            email="compras@perfiles.test",
            tax_identifier="B87654321",
        )
        cls.po = PurchaseOrder.objects.create(
            company=cls.company,
            provider=cls.provider,
            po_type=PurchaseOrderType.STOCK,
            date_ordered=date(2026, 9, 1),
            notes="Entregar por la mañana",
        )
        taxed = InventoryItem.objects.create(code="PERF-1", name="Perfil 6m", company=cls.company)
        untaxed = InventoryItem.objects.create(code="TORN-1", name="Tornillería", company=cls.company)
        iva, _ = TaxType.objects.get_or_create(
            tax_name="IVA", country="ES",
            defaults={"behavior": "add", "percentage_options": [0, 10, 21], "default_percentage": 21},
        )
        AppliedTax.objects.create(
            content_object=taxed, tax_type=iva, selected_percentage=Decimal("21"), order=0,
        )
        PurchaseOrderItem.objects.create(
            purchase_order=cls.po, inventory_item=taxed,
            quantity=Decimal("10"), unit_price=Money("20", "EUR"),
        )
        PurchaseOrderItem.objects.create(
            purchase_order=cls.po, inventory_item=untaxed,
            quantity=Decimal("2"), unit_price=Money("5", "EUR"),
        )
        cls.po.refresh_from_db()
        cls.template = ConfigurationExportTemplate.objects.create(
            company=cls.company,
            kind=ExportTemplateKind.PURCHASE_ORDER,
            name="Pedido de compra",
            html="<h1>{{ number }}</h1><p>{{ provider_name }}</p>"
                 "{% for item in items %}<li>{{ item.reference }} {{ item.line_total }}</li>{% endfor %}"
                 "<p>Total {{ total }}</p>",
        )

    def test_context_has_provider_lines_and_totals(self):
        context = PurchaseOrderExportStrategy(self.po).get_context()

        self.assertEqual(context["number"], self.po.number)
        self.assertEqual(context["provider_name"], "Perfiles del Sur S.L.")
        self.assertEqual(context["provider_tax_id"], "B87654321")
        self.assertEqual(context["notes"], "Entregar por la mañana")
        self.assertEqual(
            [(i["item_number"], i["reference"], i["line_total"]) for i in context["items"]],
            [(1, "PERF-1", 200.0), (2, "TORN-1", 10.0)],
        )
        self.assertEqual(context["subtotal"], 210.0)
        self.assertEqual(
            context["taxes"],
            [{"name": "IVA", "percentage": 21.0, "is_retention": False, "base": 200.0, "amount": 42.0}],
        )
        self.assertEqual(context["tax_amount"], 42.0)
        self.assertEqual(context["total"], 252.0)
        # Same figure the admin shows as the PO's total with tax.
        self.assertEqual(Decimal(str(context["total"])), self.po.total_amount_with_tax.amount)
        self.assertEqual(context["company_data"]["name"], "Export Co")

    def test_render_html_uses_purchase_order_strategy(self):
        html = ConfigurationExportService.render_html(self.po, self.template)

        self.assertIn(self.po.number, html)
        self.assertIn("Perfiles del Sur S.L.", html)
        self.assertIn("PERF-1", html)

    def test_kind_registry_resolves_purchase_orders_for_preview(self):
        obj = get_preview_object(ExportTemplateKind.PURCHASE_ORDER, company_id=self.company.pk)

        self.assertEqual(obj, self.po)

    def test_admin_preview_renders_company_template(self):
        admin = User.objects.create_superuser("admin", "admin@test.local", "pw")
        self.client.force_login(admin)

        response = self.client.get(reverse("admin:purchasing_purchaseorder_pdf", args=[self.po.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Perfiles del Sur S.L.")
