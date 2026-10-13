"""Quote subtotal and total follow a configuration leaving or changing quote."""
from decimal import Decimal

from django.test import TestCase

from company.models import Company
from configurator.models import Configuration, ConfigurationStatus, ProductTemplate
from sales.models import Quote


class QuoteTotalsFollowConfigurationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name="Totals Co")
        cls.product = ProductTemplate.objects.create(
            name="Window",
            company=cls.company,
        )

    def _quote(self, name):
        return Quote.objects.create(company=self.company, project_name=name)

    def _config(self, quote, amount, name):
        config = Configuration.objects.create(
            company=self.company,
            quote=quote,
            product=self.product,
            name=name,
            status=ConfigurationStatus.CONVERTED,
        )
        config.subtotal_price = Decimal(amount)
        config.total_price = Decimal(amount)
        config.save(update_fields=[
            "subtotal_price",
            "subtotal_price_currency",
            "total_price",
            "total_price_currency",
        ])
        return config

    def _amounts(self, quote):
        quote.refresh_from_db()
        return quote.subtotal_price.amount, quote.total_price.amount

    def test_deleting_a_configuration_recalculates_the_quote(self):
        quote = self._quote("Drop one")
        self._config(quote, "100.00", "Keep")
        removed = self._config(quote, "40.00", "Remove")
        quote.recalc_totals()
        self.assertEqual(self._amounts(quote), (Decimal("140.00"), Decimal("140.00")))

        removed.delete()

        self.assertEqual(self._amounts(quote), (Decimal("100.00"), Decimal("100.00")))

    def test_queryset_delete_recalculates_the_quote(self):
        quote = self._quote("Bulk delete")
        self._config(quote, "80.00", "Keep")
        removed = self._config(quote, "20.00", "Remove")
        quote.recalc_totals()

        Configuration.objects.filter(pk=removed.pk).delete()

        self.assertEqual(self._amounts(quote), (Decimal("80.00"), Decimal("80.00")))

    def test_moving_a_configuration_updates_both_quotes(self):
        source = self._quote("Source")
        target = self._quote("Target")
        self._config(source, "100.00", "Stays")
        moved = self._config(source, "50.00", "Moves")
        source.recalc_totals()
        target.recalc_totals()

        moved.quote = target
        moved.save(update_fields=["quote"])

        self.assertEqual(self._amounts(source), (Decimal("100.00"), Decimal("100.00")))
        self.assertEqual(self._amounts(target), (Decimal("50.00"), Decimal("50.00")))

    def test_clearing_the_quote_removes_its_amount(self):
        quote = self._quote("Unlink")
        config = self._config(quote, "75.00", "Loose")
        quote.recalc_totals()

        config.quote = None
        config.save()

        self.assertEqual(self._amounts(quote), (Decimal("0.00"), Decimal("0.00")))

    def test_unrelated_save_leaves_stored_totals_alone(self):
        quote = self._quote("Rename")
        config = self._config(quote, "30.00", "Named")
        quote.subtotal_price = Decimal("999.00")
        quote.total_price = Decimal("999.00")
        quote.save(update_fields=[
            "subtotal_price",
            "subtotal_price_currency",
            "total_price",
            "total_price_currency",
        ])

        config.name = "Renamed"
        config.save(update_fields=["name"])

        self.assertEqual(self._amounts(quote), (Decimal("999.00"), Decimal("999.00")))
