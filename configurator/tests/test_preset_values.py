from django.core.exceptions import ValidationError
from django.test import TestCase

from configurator.models import ConfigurationPreset, ParameterType, ProductParameter, ProductTemplate
from sales.tests.factories import make_company


class PresetWithValuesTests(TestCase):
    """A preset completed with a customer's values: only its open parameters, valid for their type."""

    @classmethod
    def setUpTestData(cls):
        company = make_company(name="Fistex")
        template = ProductTemplate.objects.create(company=company, name="CA/TA")
        for order, (name, kind, options) in enumerate([
            ("VOLUMEN", ParameterType.SELECT, [160, 200]),
            ("ALTURA", ParameterType.NUMBER, None),
            ("SOLAR", ParameterType.BOOLEAN, None),
            ("RECUBRIMIENTO", ParameterType.SELECT, ["F.Skay"]),
        ]):
            ProductParameter.objects.create(product=template, name=name, label=name.title(), type=kind, options=options, order=order)
        ProductParameter.objects.create(product=template, name="NOTA", label="Nota", type=ParameterType.TEXT, required=False)
        cls.preset = ConfigurationPreset.objects.create(
            company=company, product=template, name="CA/TA a medida", parameter_values={"RECUBRIMIENTO": "F.Skay"},
        )

    def test_open_parameters_are_merged_with_the_presets_values(self):
        values = self.preset.with_values({"VOLUMEN": 200, "ALTURA": " 1850.5 ", "SOLAR": "True"})

        self.assertEqual(values, {"RECUBRIMIENTO": "F.Skay", "VOLUMEN": "200", "ALTURA": "1850.5", "SOLAR": "True"})

    def test_invalid_values_are_reported_per_parameter(self):
        with self.assertRaises(ValidationError) as caught:
            self.preset.with_values({
                "VOLUMEN": "350",  # not an option
                "ALTURA": "1,85",  # not a number
                "SOLAR": "maybe",
                "RECUBRIMIENTO": "F.Skay",  # fixed by the preset
                "NOTA": "x",  # optional parameters aren't offered, as in the preset wizard
            })

        self.assertEqual(set(caught.exception.message_dict), {"VOLUMEN", "ALTURA", "SOLAR", "RECUBRIMIENTO", "NOTA"})

    def test_every_open_parameter_is_required(self):
        with self.assertRaises(ValidationError) as caught:
            self.preset.with_values({"VOLUMEN": "160"})

        self.assertEqual(set(caught.exception.message_dict), {"ALTURA", "SOLAR"})
