from decimal import Decimal

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from djmoney.money import Money

from company.models import Company
from configurator.models import BOMRule, Configuration, ProductTemplate
from materials.models import Category, Component, ComponentPriceRule, Material


class ConfigurationBOMQueryTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        company = Company.objects.create(name="BOM Query Company")
        category = Category.objects.create(company=company, name="Profiles")
        product = ProductTemplate.objects.create(company=company, name="Window")
        cls.configuration = Configuration.objects.create(
            company=company, product=product, name="Window configuration"
        )

        for index in range(3):
            material = Material.objects.create(
                company=company,
                category=category,
                reference_code=f"BOM-QUERY-{index}",
                name=f"Profile {index}",
                cost_per_unit=Money(Decimal(index + 1), "EUR"),
            )
            component = Component.objects.create(
                company=company,
                name=f"Component {index}",
                unit_price_expression="base_cost",
            )
            component.materials.add(material)
            ComponentPriceRule.objects.create(
                component=component,
                condition="True",
                unit_price_expression="base_cost",
                unit_price=Money(0, "EUR"),
            )
            BOMRule.objects.create(
                company=company,
                product=product,
                component=component,
                quantity_expression="1",
            )

    def test_generate_bom_batches_component_materials_and_price_rules(self):
        with CaptureQueriesContext(connection) as queries:
            self.configuration.generate_bom()

        material_queries = [
            query for query in queries
            if 'FROM "materials_material"' in query["sql"]
        ]
        price_rule_queries = [
            query for query in queries
            if 'FROM "materials_componentpricerule"' in query["sql"]
        ]
        self.assertEqual(len(material_queries), 1)
        self.assertEqual(len(price_rule_queries), 1)
        self.assertEqual(
            list(
                self.configuration.items.order_by("component__name").values_list(
                    "unit_price", flat=True
                )
            ),
            [Decimal("1"), Decimal("2"), Decimal("3")],
        )

    def test_pricing_context_does_not_load_each_item_component(self):
        self.configuration.generate_bom()

        with CaptureQueriesContext(connection) as queries:
            context = self.configuration.get_pricing_context()

        component_queries = [
            query for query in queries
            if 'FROM "materials_component"' in query["sql"]
        ]
        self.assertEqual(len(component_queries), 0)
        self.assertEqual(context["material_cost"], 6.0)
