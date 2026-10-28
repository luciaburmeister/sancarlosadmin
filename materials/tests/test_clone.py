"""Tests for Material.clone() and Component.clone(), backing the admin
"Duplicate selected" actions."""
from django.test import TestCase

from company.models import Company
from materials.models import Category, Component, ComponentPriceRule, Material


class MaterialCloneTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name="Clone Co")
        cls.category = Category.objects.create(name="Clone Category", company=cls.company)
        cls.material = Material.objects.create(
            name="Original Material",
            reference_code="REF-1",
            rule_key="original_key",
            category=cls.category,
            cost_per_unit=10,
            company=cls.company,
        )

    def test_clone_is_an_independent_copy_with_suffixed_name_and_unique_codes(self):
        clone = self.material.clone()

        self.assertNotEqual(clone.pk, self.material.pk)
        self.assertEqual(clone.name, "Original Material (copy)")
        self.assertEqual(clone.reference_code, "REF-1-copy")
        self.assertEqual(clone.rule_key, "original_key_copy")
        self.assertEqual(clone.category, self.category)
        self.assertEqual(clone.cost_per_unit, self.material.cost_per_unit)

    def test_cloning_twice_keeps_reference_code_and_rule_key_unique(self):
        first_clone = self.material.clone()
        second_clone = self.material.clone()

        self.assertNotEqual(first_clone.reference_code, second_clone.reference_code)
        self.assertEqual(second_clone.reference_code, "REF-1-copy2")
        self.assertEqual(second_clone.rule_key, "original_key_copy2")

    def test_cloning_does_not_touch_the_original(self):
        self.material.clone()
        self.material.refresh_from_db()

        self.assertEqual(self.material.reference_code, "REF-1")
        self.assertEqual(self.material.rule_key, "original_key")

    def test_clone_without_rule_key_leaves_it_blank(self):
        self.material.rule_key = None
        self.material.save()

        clone = self.material.clone()

        self.assertIsNone(clone.rule_key)


class ComponentCloneTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name="Clone Co")
        cls.category = Category.objects.create(name="Clone Category", company=cls.company)
        cls.material = Material.objects.create(
            name="Steel",
            reference_code="REF-STEEL",
            rule_key="steel",
            category=cls.category,
            cost_per_unit=5,
            company=cls.company,
        )

    def _make_source(self):
        component = Component.objects.create(
            name="Original Component",
            code="C-1",
            unit_price_expression="base_cost * 2",
            company=self.company,
        )
        component.materials.add(self.material)
        ComponentPriceRule.objects.create(
            component=component,
            condition="True",
            unit_price=3,
            priority=0,
            is_fallback=True,
        )
        component.save()
        return component

    def test_clone_is_an_independent_copy_with_suffixed_name(self):
        source = self._make_source()
        clone = source.clone()

        self.assertNotEqual(clone.pk, source.pk)
        self.assertEqual(clone.name, "Original Component (copy)")
        self.assertEqual(clone.code, source.code)
        self.assertEqual(clone.unit_price_expression, source.unit_price_expression)

    def test_clone_copies_materials_and_price_rules(self):
        source = self._make_source()
        clone = source.clone()

        self.assertEqual(list(clone.materials.all()), [self.material])
        self.assertEqual(clone.price_rules.count(), 1)
        self.assertEqual(clone.cost_per_unit, source.cost_per_unit)

    def test_cloning_does_not_touch_the_original(self):
        source = self._make_source()
        source.clone()
        source.refresh_from_db()

        self.assertEqual(source.price_rules.count(), 1)
        self.assertEqual(list(source.materials.all()), [self.material])
