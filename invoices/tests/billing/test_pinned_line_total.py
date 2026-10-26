"""A quote inventory line whose total was set by hand (override_line_total)
keeps that exact total on invoices and delivery orders: its discount % is
only that total rounded to two decimals, so recomputing from it drifts.

26 × 1,002.18 = 26,056.68 pinned at 16,933.80 is a 35.0117 % discount, shown
as 35.01 %, which would recompute to 16,934.24."""
from datetime import date
from decimal import Decimal

from django.test import TestCase
from djmoney.money import Money

from inventory.models import InventoryItem
from invoices.forms.wizard_forms import WizardItemForm
from invoices.services.delivery_order_service import DeliveryOrderService
from invoices.services.project_file_invoice_service import ProjectFileInvoiceService
from invoices.tests.factories import (
    create_invoice,
    make_client,
    make_company,
    make_delivery_order,
    make_project_file,
    make_quote,
)
from sales.models import QuoteInventoryItem

PINNED = Decimal("16933.80")


class PinnedLineTotalTest(TestCase):
    def setUp(self):
        self.company = make_company(name="Termos SL")
        self.client_obj = make_client(company=self.company, name="Cliente SA")
        self.project_file = make_project_file(
            company=self.company, client=self.client_obj, name="Obra",
        )
        self.quote = make_quote(project_file=self.project_file, project_name="Obra")
        self.inv_item = QuoteInventoryItem.objects.create(
            quote=self.quote,
            inventory_item=InventoryItem.objects.create(
                name="Termosifón Mod. ELITE 160 BS (SK)", company=self.company,
            ),
            quantity=Decimal("26"),
            unit_price=Decimal("1002.18"),
            override_line_total=Money(PINNED, "EUR"),
        )
        self.quote.recalc_totals()
        self.inv_item.refresh_from_db()

    def _summary(self):
        return ProjectFileInvoiceService.get_invoiceable_summary(self.project_file)

    def _create_invoice(self, items_data):
        return create_invoice(
            project_file=self.project_file, items=items_data,
            date_issued=date(2026, 10, 14),
        )

    def _deliver(self, quantity):
        (entry,) = DeliveryOrderService.get_deliverable_items(self.project_file)
        return make_delivery_order(
            project_file=self.project_file,
            items=[{
                "source_content_type_id": entry["source_content_type_id"],
                "source_object_id": entry["source_object_id"],
                "description": entry["description"],
                "quantity": quantity,
                "unit_price": entry["unit_price"],
                "discount_percentage": entry["discount_percentage"],
            }],
        )

    def test_the_quote_line_shows_the_rounded_discount(self):
        self.assertEqual(self.inv_item.discount, Decimal("35.01"))
        self.assertEqual(self.inv_item.line_total.amount, PINNED)

    def test_invoice_line_keeps_the_quote_total(self):
        invoice = self._create_invoice(
            ProjectFileInvoiceService.build_items_for_remaining(self._summary())
        )

        [line] = invoice.items.all()
        self.assertEqual(line.quantity, Decimal("26"))
        self.assertEqual(line.unit_price.amount, Decimal("1002.18"))
        self.assertEqual(line.discount_percentage, Decimal("35.01"))
        self.assertEqual(line.line_total.amount, PINNED)
        self.assertEqual(line.discount_amount.amount, Decimal("9122.88"))
        self.assertEqual(invoice.subtotal_amount.amount, PINNED)

    def test_editing_the_line_drops_the_pin(self):
        invoice = self._create_invoice(
            ProjectFileInvoiceService.build_items_for_remaining(self._summary())
        )
        [line] = invoice.items.all()

        line.quantity = Decimal("25")
        line.save()
        line.refresh_from_db()

        self.assertIsNone(line.override_line_total)
        self.assertEqual(line.line_total.amount, Decimal("16282.92"))  # 25 × 1,002.18 × 0.6499

    def test_a_line_whose_discount_reproduces_its_total_is_not_pinned(self):
        self.inv_item.override_line_total = None
        self.inv_item.discount = Decimal("10")
        self.inv_item.save()

        [item] = ProjectFileInvoiceService.build_items_for_ratio(self._summary(), Decimal("1"))

        self.assertIsNone(item["override_line_total"])

    def test_wizard_row_keeps_the_pin_until_edited(self):
        initial = {
            "source_id": f"inv_item_{self.inv_item.pk}", "description": "Termosifón",
            "quantity": Decimal("26"), "unit_price": Decimal("1002.18"),
            "discount_percentage": Decimal("35.01"), "override_line_total": PINNED,
        }
        posted = {key: str(value) for key, value in initial.items() if key != "override_line_total"}

        untouched = WizardItemForm(data=posted, initial=initial)
        edited = WizardItemForm(data={**posted, "unit_price": "1000.00"}, initial=initial)

        self.assertTrue(untouched.is_valid(), untouched.errors)
        self.assertTrue(edited.is_valid(), edited.errors)
        self.assertEqual(untouched.cleaned_data["override_line_total"], PINNED)
        self.assertIsNone(edited.cleaned_data["override_line_total"])

    def test_delivery_order_and_its_invoice_keep_the_quote_total(self):
        delivery_order = self._deliver(Decimal("26"))

        [do_line] = delivery_order.items.all()
        invoice = DeliveryOrderService.create_invoice_from_delivery_orders(
            [delivery_order], self.company, self.client_obj, date(2026, 10, 14),
        )

        self.assertEqual(do_line.line_total.amount, PINNED)
        [line] = invoice.items.all()
        self.assertEqual(line.line_total.amount, PINNED)
        self.assertEqual(line.discount_percentage, Decimal("35.01"))

    def test_partial_deliveries_add_up_to_the_quote_total(self):
        first = self._deliver(Decimal("10"))
        rest = self._deliver(Decimal("16"))

        [first_line] = first.items.all()
        [rest_line] = rest.items.all()
        self.assertEqual(first_line.line_total.amount, Decimal("6513.00"))  # 16,933.80 × 10 / 26
        self.assertEqual(first_line.line_total.amount + rest_line.line_total.amount, PINNED)
