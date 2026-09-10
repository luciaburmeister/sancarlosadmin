"""
Tests that a Quote export lists its inventory items in the user-defined order
held by ``QuoteInventoryItem.order`` (the drag-and-drop field on the admin
inline), rather than in whatever order the database returns them.
"""
from decimal import Decimal

from django.test import TestCase

from company.models import Company
from configurator.services.export.quote import QuoteExportStrategy
from inventory.models import InventoryItem
from sales.models import Client, Quote, QuoteInventoryItem


class QuoteExportInventoryItemOrderingTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name="Export Order Co")
        cls.client_obj = Client.objects.create(name="Acme", company=cls.company)

    def _make_quote(self):
        return Quote.objects.create(
            client=self.client_obj,
            company=self.company,
            project_name="Export Ordering Test",
        )

    def _add_line(self, quote, name, order):
        inv = InventoryItem.objects.create(
            name=name, company=self.company, selling_price=Decimal("10.00"),
        )
        return QuoteInventoryItem.objects.create(
            quote=quote,
            inventory_item=inv,
            quantity=Decimal("1"),
            unit_price=Decimal("10.00"),
            order=order,
        )

    def _exported_lines(self, quote):
        """The export's inventory lines, identified by primary key."""
        context = QuoteExportStrategy(quote).get_context(include_rendered_templates=False)
        return [item["id"] for item in context["inventory_items"]]

    def test_export_follows_order_field_not_creation_order(self):
        """Lines come out by ``order``, even when that contradicts insertion order."""
        quote = self._make_quote()
        third = self._add_line(quote, "Third", order=3)
        first = self._add_line(quote, "First", order=1)
        second = self._add_line(quote, "Second", order=2)

        self.assertEqual(
            self._exported_lines(quote), [first.pk, second.pk, third.pk]
        )

    def test_reordering_lines_changes_the_export(self):
        """Moving a line in the admin is reflected on the next export."""
        quote = self._make_quote()
        first = self._add_line(quote, "First", order=1)
        second = self._add_line(quote, "Second", order=2)

        self.assertEqual(self._exported_lines(quote), [first.pk, second.pk])

        first.order = 2
        first.save(update_fields=["order"])
        second.order = 1
        second.save(update_fields=["order"])

        self.assertEqual(self._exported_lines(quote), [second.pk, first.pk])

    def test_ties_fall_back_to_creation_order(self):
        """Rows sharing an ``order`` — e.g. every row predating the field, which
        defaults to 0 — keep a stable creation order instead of an arbitrary one.
        """
        quote = self._make_quote()
        lines = [self._add_line(quote, name, order=0)
                 for name in ["Alpha", "Beta", "Gamma", "Delta"]]

        self.assertEqual(
            self._exported_lines(quote), [line.pk for line in lines]
        )

    def test_unordered_rows_precede_explicitly_ordered_ones(self):
        """A never-dragged line (order=0) sorts ahead of positioned lines."""
        quote = self._make_quote()
        positioned = self._add_line(quote, "Positioned", order=5)
        untouched = self._add_line(quote, "Untouched", order=0)

        self.assertEqual(
            self._exported_lines(quote), [untouched.pk, positioned.pk]
        )
