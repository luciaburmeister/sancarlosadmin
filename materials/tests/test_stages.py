from django.test import TestCase
from django.core.exceptions import ValidationError
from company.models import Company
from materials.models import Material, Component, Category, Provider
from configurator.models import ProductTemplate, Configuration, ConfigurationItem
from decimal import Decimal

class StagesValidationTestCase(TestCase):
    def setUp(self):
        self.company = Company.objects.create(
            name="Test Company",
            production_stages=["Cutting", "Assembly"]
        )
        self.provider = Provider.objects.create(name="Test Provider", company=self.company)
        self.category = Category.objects.create(name="Test Category", company=self.company)
        self.material = Material.objects.create(
            reference_code="M001",
            name="Test Material",
            category=self.category,
            provider=self.provider,
            company=self.company,
            cost_per_unit=100
        )

    def test_component_stages_validation(self):
        # Valid stages (subset)
        component = Component(
            # material=self.material, # Removed
            name="Valid Component",
            company=self.company,
            production_stages=["Cutting"],
            unit_price_expression="base_cost"
        )
        component.clean() # Should not raise
        component.save()

        # Invalid stages (not a subset)
        invalid_component = Component(
            # material=self.material, # Removed
            name="Invalid Component",
            company=self.company,
            production_stages=["Cutting", "Painting"],
            unit_price_expression="base_cost"
        )
        with self.assertRaises(ValidationError) as cm:
            invalid_component.clean()
        self.assertIn("production_stages", cm.exception.message_dict)

    def test_product_template_stages_validation(self):
        # Valid stages
        product = ProductTemplate(
            company=self.company,
            name="Valid Product",
            production_stages=["Assembly"]
        )
        product.clean() # Should not raise
        product.save()

        # Invalid stages
        invalid_product = ProductTemplate(
            company=self.company,
            name="Invalid Product",
            production_stages=["Painting"]
        )
        with self.assertRaises(ValidationError) as cm:
            invalid_product.clean()
        self.assertIn("production_stages", cm.exception.message_dict)

    def test_configuration_item_add_on_stages_validation(self):
        product = ProductTemplate.objects.create(
            company=self.company,
            name="Test Product",
            production_stages=["Cutting"]
        )
        config = Configuration.objects.create(
            company=self.company,
            name="Test Config",
            product=product
        )
        
        # Valid add-on stage
        item = ConfigurationItem(
            configuration=config,
            component=None,
            is_template_item=False,
            production_stages=["Cutting"],
            quantity=1,
            unit_price=10
        )
        item.clean() # Should not raise
        
        # Invalid add-on stage
        invalid_item = ConfigurationItem(
            configuration=config,
            component=None,
            is_template_item=False,
            production_stages=["Painting"],
            quantity=1,
            unit_price=10
        )
        with self.assertRaises(ValidationError) as cm:
            invalid_item.clean()
        self.assertIn("production_stages", cm.exception.message_dict)
