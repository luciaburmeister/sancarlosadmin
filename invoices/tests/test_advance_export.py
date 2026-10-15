"""
Advance invoices in the sales exports and in the DjangoQL search:

- the header export tells an advance from an invoice (document type, %)
  and shows what each invoice deducts: the amount and the advance numbers;
- the line export flags deduction lines and names the advance invoice each
  advance line or deduction line belongs to;
- both exports read the advances in bulk, not once per row;
- ``document_type`` is searchable.
"""
from decimal import Decimal

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import translation
from django.utils.formats import sanitize_separators
from djangoql.queryset import apply_search

from general.djangoql.fields import localized_name
from general.djangoql.schema import RegisteredDjangoQLSchema
from invoices.models import SalesInvoice, SalesInvoiceDocumentType, SalesInvoiceItem
from invoices.resources import SalesInvoiceItemResource, SalesInvoiceResource
from invoices.services.advances import AdvanceDeductionService
from invoices.tests.advance_fixtures import AdvanceExpedienteMixin


def _rows(resource, queryset):
    """Export ``queryset``: one {field name: exported value} per row, so the
    tests never depend on the (translated) column headers."""
    dataset = resource.export(queryset=queryset)
    names = [resource.get_field_name(field) for field in resource.get_export_fields()]
    return [dict(zip(names, row)) for row in dataset]


def _query_count(resource, queryset):
    with CaptureQueriesContext(connection) as ctx:
        _rows(resource, queryset)
    return len(ctx.captured_queries)


class _AdvanceExportTestBase(AdvanceExpedienteMixin, TestCase):
    """A final 30 % advance (INV-2026-00012) of the 1,000.00 expediente, and
    the pro forma billing its goods, which deducts the 300.00."""

    def setUp(self):
        super().setUp()
        self.advance, self.rest = self.bill_with_advance(self.project_file, "INV-2026-00012")
        self.advance_line = self.advance.items.get()

    def bill_with_advance(self, project_file, number):
        """A final 30 % advance numbered ``number`` on ``project_file``, and the
        pro forma billing all its goods, which deducts the advance."""
        self.project_file = project_file
        advance, _line = self.make_advance(Decimal("300.00"), "30")
        SalesInvoice.objects.filter(pk=advance.pk).update(invoice_number=number)
        rest = self.rest_invoice()
        advance.refresh_from_db()
        return advance, rest


class SalesInvoiceExportTests(_AdvanceExportTestBase):
    def header_row(self, invoice):
        [row] = _rows(SalesInvoiceResource(), SalesInvoice.objects.filter(pk=invoice.pk))
        return row

    def test_an_advance_invoice_is_told_apart(self):
        row = self.header_row(self.advance)

        self.assertEqual(row["document_type"], SalesInvoiceDocumentType.ADVANCE)
        # Rendered in the active locale, like every other decimal column.
        self.assertEqual(Decimal(sanitize_separators(row["advance_percentage"])), Decimal("30"))
        # An advance deducts nothing itself.
        self.assertEqual(row["advances_deducted_value"], Decimal("0.00"))
        self.assertEqual(row["advance_invoice_numbers"], "")

    def test_the_rest_invoice_shows_the_advance_it_deducts(self):
        row = self.header_row(self.rest)

        self.assertEqual(row["document_type"], SalesInvoiceDocumentType.INVOICE)
        self.assertEqual(row["advance_percentage"], "")
        self.assertEqual(row["advances_deducted_value"], Decimal("300.00"))
        self.assertEqual(row["advance_invoice_numbers"], "INV-2026-00012")
        # The goods' base is Subtotal + Discount + Advances Deducted.
        self.assertEqual(
            Decimal(str(row["subtotal_amount_value"]))
            + Decimal(str(row["discount_amount_value"]))
            + row["advances_deducted_value"],
            Decimal("1000.00"),
        )

    def test_each_advance_is_listed_once_in_the_order_deducted(self):
        AdvanceDeductionService.remove(self.rest)
        second, second_line = self.make_advance(Decimal("200.00"), "20")
        SalesInvoice.objects.filter(pk=second.pk).update(invoice_number="INV-2026-00013")
        # The first advance in two lines, then the second one.
        self.deduct(self.rest, self.advance_line, Decimal("100.00"))
        self.deduct(self.rest, self.advance_line, Decimal("50.00"))
        self.deduct(self.rest, second_line, Decimal("200.00"))

        row = self.header_row(self.rest)

        self.assertEqual(row["advances_deducted_value"], Decimal("350.00"))
        self.assertEqual(row["advance_invoice_numbers"], "INV-2026-00012, INV-2026-00013")

    def test_new_columns_sit_before_the_amounts_they_explain(self):
        resource = SalesInvoiceResource()
        names = [resource.get_field_name(field) for field in resource.get_export_fields()]
        self.assertEqual(names.index("document_type"), names.index("status") + 1)
        self.assertEqual(names.index("advance_percentage"), names.index("document_type") + 1)
        self.assertLess(names.index("advances_deducted_value"), names.index("discount_amount_value"))
        self.assertLess(names.index("discount_amount_value"), names.index("subtotal_amount_value"))

    def test_advances_are_read_in_bulk(self):
        resource = SalesInvoiceResource()
        one = _query_count(resource, SalesInvoice.objects.filter(pk=self.rest.pk))

        second_project_file, _quote, _config = self.make_expediente("Obra 2")
        _advance, second_rest = self.bill_with_advance(second_project_file, "INV-2026-00020")
        two = _query_count(
            resource, SalesInvoice.objects.filter(pk__in=[self.rest.pk, second_rest.pk]),
        )

        self.assertEqual(one, two)


class SalesInvoiceItemExportTests(_AdvanceExportTestBase):
    def item_rows(self, *invoices):
        items = SalesInvoiceItem.objects.filter(invoice__in=invoices).order_by("pk")
        return {
            item.pk: row
            for item, row in zip(items, _rows(SalesInvoiceItemResource(), items))
        }

    def test_advance_and_deduction_lines_name_their_advance(self):
        rows = self.item_rows(self.advance, self.rest)
        deduction = self.rest.items.get(advance_line__isnull=False)
        goods = self.rest.items.filter(advance_line__isnull=True)

        self.assertIs(rows[self.advance_line.pk]["is_advance_deduction"], False)
        self.assertEqual(rows[self.advance_line.pk]["advance_invoice_number"], "INV-2026-00012")
        self.assertIs(rows[deduction.pk]["is_advance_deduction"], True)
        self.assertEqual(rows[deduction.pk]["advance_invoice_number"], "INV-2026-00012")
        self.assertTrue(goods.exists())
        for line in goods:
            self.assertIs(rows[line.pk]["is_advance_deduction"], False)
            self.assertEqual(rows[line.pk]["advance_invoice_number"], "")

    def test_a_pro_forma_advance_goes_by_its_pro_forma_number(self):
        SalesInvoice.objects.filter(pk=self.advance.pk).update(invoice_number=None)
        self.advance.refresh_from_db()

        rows = self.item_rows(self.advance)

        self.assertEqual(
            rows[self.advance_line.pk]["advance_invoice_number"], self.advance.proforma_number,
        )

    def test_lines_are_read_in_bulk(self):
        resource = SalesInvoiceItemResource()
        one = _query_count(resource, SalesInvoiceItem.objects.filter(invoice=self.rest))

        second_project_file, _quote, _config = self.make_expediente("Obra 2")
        _advance, second_rest = self.bill_with_advance(second_project_file, "INV-2026-00020")
        two = _query_count(
            resource, SalesInvoiceItem.objects.filter(invoice__in=[self.rest, second_rest]),
        )

        self.assertEqual(one, two)


class DocumentTypeSearchTests(_AdvanceExportTestBase):
    def search(self, query, lang="en"):
        with translation.override(lang):
            qs = apply_search(SalesInvoice.objects.all(), query, RegisteredDjangoQLSchema)
            return set(qs.values_list("pk", flat=True))

    def test_advances_can_be_searched(self):
        self.assertEqual(self.search('document_type = "advance"'), {self.advance.pk})
        self.assertEqual(self.search('document_type = "invoice"'), {self.rest.pk})
        self.assertEqual(self.search('document_type != "advance"'), {self.rest.pk})

    def test_the_localized_name_searches_the_same(self):
        with translation.override("es"):
            name = localized_name("document_type")
        self.assertEqual(self.search(f'{name} = "advance"', lang="es"), {self.advance.pk})
