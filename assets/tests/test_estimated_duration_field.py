"""
assets/tests/test_estimated_duration_field.py

EstimatedDurationChoiceField (assets/forms.py): a duration-preset select used
by both AssetPlanningAdmin and AssetPlanningRequestAdmin. Regression coverage
for a 500 on save — Django's Form.changed_data calls field.has_changed(initial,
data), which runs _coerce on *both* sides; initial is the model's raw
timedelta (not a choice string), which used to blow up int(timedelta(...)).

Run just this file:
    python manage.py test assets.tests.test_estimated_duration_field
"""
from datetime import timedelta

from django.test import SimpleTestCase

from assets.forms import EstimatedDurationChoiceField


class EstimatedDurationChoiceFieldTests(SimpleTestCase):
    def test_has_changed_is_false_when_the_submitted_value_matches_initial(self):
        field = EstimatedDurationChoiceField()
        self.assertFalse(field.has_changed(timedelta(hours=1), "60"))

    def test_has_changed_is_true_on_a_real_change(self):
        field = EstimatedDurationChoiceField()
        self.assertTrue(field.has_changed(timedelta(hours=1), "30"))

    def test_has_changed_handles_a_blank_initial(self):
        field = EstimatedDurationChoiceField()
        self.assertTrue(field.has_changed(None, "60"))
        self.assertFalse(field.has_changed(None, ""))

    def test_coerce_passes_through_an_existing_timedelta(self):
        value = timedelta(minutes=45)
        self.assertEqual(EstimatedDurationChoiceField._coerce(value), value)

    def test_coerce_converts_submitted_minutes_string(self):
        self.assertEqual(EstimatedDurationChoiceField._coerce("45"), timedelta(minutes=45))

    def test_coerce_treats_blank_as_none(self):
        self.assertIsNone(EstimatedDurationChoiceField._coerce(""))
        self.assertIsNone(EstimatedDurationChoiceField._coerce(None))
