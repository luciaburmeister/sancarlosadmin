"""
core/tests/test_dynamic_fields.py

Run:
    python manage.py test core.tests.test_dynamic_fields
"""
from django import forms
from django.test import SimpleTestCase

from core.forms.dynamic_fields import DynamicFieldBuilder, json_schema_fragment


class JsonSchemaFragmentTest(SimpleTestCase):

    def test_multiselect_is_an_array_of_options(self):
        self.assertEqual(
            json_schema_fragment("multiselect", options=["a", "b"]),
            {"type": "array", "items": {"enum": ["a", "b"]}, "uniqueItems": True},
        )


class DynamicFieldBuilderTest(SimpleTestCase):

    def test_multiselect_field(self):
        field = DynamicFieldBuilder().build(field_type="multiselect", label="Tags", required=False, options=["a", "b"])
        self.assertIsInstance(field, forms.MultipleChoiceField)
        self.assertEqual(field.clean(["a"]), ["a"])
        with self.assertRaises(forms.ValidationError):
            field.clean(["z"])

    def test_number_bounds_and_help_text(self):
        field = DynamicFieldBuilder().build(
            field_type="number", label="Weight", required=False, min_value=0, max_value=10, help_text="kg",
        )
        self.assertEqual(field.help_text, "kg")
        with self.assertRaises(forms.ValidationError):
            field.clean("-1")
