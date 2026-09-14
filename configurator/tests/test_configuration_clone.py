"""Tests for Configuration.clone(), backing the admin "Duplicate" action."""
from django.test import TestCase

from company.models import Company
from configurator.models import (
    Configuration,
    ConfigurationParameterValue,
    ConfigurationStatus,
    ParameterType,
    ProductParameter,
    ProductTemplate,
)


class ConfigurationCloneTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name="Clone Co")
        cls.product = ProductTemplate.objects.create(
            name="Clone Product",
            company=cls.company,
        )
        cls.width = ProductParameter.objects.create(
            product=cls.product, name="width", label="Width", type=ParameterType.NUMBER,
        )

    def _make_source(self, status=ConfigurationStatus.CONVERTED):
        config = Configuration.objects.create(
            company=self.company,
            product=self.product,
            name="Original",
            status=status,
            notes="Handle with care",
        )
        ConfigurationParameterValue.objects.create(
            configuration=config, parameter=self.width, value="150", order=self.width.order,
        )
        return config

    def test_clone_is_an_independent_draft_with_a_suffixed_name(self):
        source = self._make_source()
        clone = source.clone()

        self.assertNotEqual(clone.pk, source.pk)
        self.assertEqual(clone.name, "Original (copy)")
        self.assertEqual(clone.status, ConfigurationStatus.DRAFT)
        self.assertEqual(clone.notes, "Handle with care")

    def test_clone_copies_parameter_values(self):
        source = self._make_source()
        clone = source.clone()

        self.assertEqual(clone.param_values.get(parameter=self.width).value, "150")

    def test_cloning_does_not_touch_the_original(self):
        source = self._make_source(status=ConfigurationStatus.CONVERTED)
        source.clone()
        source.refresh_from_db()

        self.assertEqual(source.status, ConfigurationStatus.CONVERTED)
        self.assertEqual(source.param_values.get(parameter=self.width).value, "150")
