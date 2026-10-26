from decimal import Decimal

from django.test import SimpleTestCase, TestCase

from invoices import line_sources
from invoices.tests.advance_fixtures import AdvanceExpedienteMixin


class ParseTest(SimpleTestCase):
    def test_round_trip_of_every_prefix(self):
        for prefix in line_sources.PREFIXES:
            with self.subTest(prefix=prefix):
                self.assertEqual(line_sources.parse(line_sources.source_id(prefix, 42)), (prefix, 42))

    def test_unknown_or_malformed_source_ids(self):
        for value in (None, "", "manual", "config_", "config_abc", "source_12_3"):
            with self.subTest(value=value):
                self.assertEqual(line_sources.parse(value), (None, None))

    def test_unknown_prefix_cannot_be_built(self):
        with self.assertRaises(ValueError):
            line_sources.source_id("configuration_", 1)

    def test_pk_of_only_matches_its_prefix(self):
        self.assertEqual(line_sources.pk_of("inv_item_7", line_sources.INV_ITEM), 7)
        self.assertIsNone(line_sources.pk_of("quote_item_7", line_sources.INV_ITEM))


class QuoteLookupTest(AdvanceExpedienteMixin, TestCase):
    def test_quote_of_each_kind_of_line(self):
        direct = self.add_config(self.quote, Decimal("10.00"))
        type(direct).objects.filter(pk=direct.pk).update(quote=None)

        sources = [
            line_sources.source_id(line_sources.CONFIG, self.config.pk),
            line_sources.source_id(line_sources.DISCOUNT_QUOTE, self.quote.pk),
            line_sources.source_id(line_sources.DIRECT, direct.pk),
            line_sources.source_id(line_sources.WORK_ENTRY, 1),
            "manual",
        ]
        self.assertEqual(line_sources.quote_ids_by_source(sources), {
            sources[0]: self.quote.pk,
            sources[1]: self.quote.pk,
        })

    def test_quote_source_ids_cover_its_lines(self):
        self.assertIn(
            line_sources.source_id(line_sources.CONFIG, self.config.pk),
            line_sources.quote_source_ids(self.quote),
        )
