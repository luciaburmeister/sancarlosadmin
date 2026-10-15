from django.test import TestCase
from django.core.exceptions import ValidationError
from materials.models import Material, Category, Component
from materials.validators import validate_rule_key
from company.models import Company

class RuleKeyTestCase(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Test Company")
        self.category = Category.objects.create(
            name="Test Category", 
            company=self.company
        )

    def test_validate_rule_key(self):
        # Valid keys
        validate_rule_key("steel_4058")
        validate_rule_key("m1")
        validate_rule_key("Aluminum_Profile")
        
        # Invalid keys
        with self.assertRaises(ValidationError):
            validate_rule_key("123start") # Starts with number
        with self.assertRaises(ValidationError):
            validate_rule_key("has-dash") # Contains dash
        with self.assertRaises(ValidationError):
            validate_rule_key("has space") # Contains space
            
    def test_pricing_uses_rule_key(self):
        material = Material.objects.create(
            name="Test Material",
            reference_code="REF_OLD", # Should not be used in pricing context
            rule_key="mat_new_key",   # Should be used
            category=self.category,
            cost_per_unit=10.0,
            company=self.company
        )
        
        component = Component.objects.create(
            name="Test Component",
            company=self.company
        )
        component.materials.add(material)
        component.save() # Trigger compute
        
        context = component.get_pricing_context()
        
        # Check that prices dict uses rule_key
        self.assertIn("mat_new_key", context["prices"])
        self.assertEqual(context["prices"]["mat_new_key"], 10.0)
        
        # Ensure reference_code is NOT used as key
        self.assertNotIn("REF_OLD", context["prices"])
