"""
assets/tests/test_time_choice_field.py

TimeChoiceField (assets/forms.py): an hour-of-day preset select used by both
AssetPlanningAdmin (scheduled_time) and AssetPlanningRequestAdmin
(needed_time). Same has_changed()-with-a-raw-model-value shape as
EstimatedDurationChoiceField (see test_estimated_duration_field.py) — guard
against the same class of bug.

Run just this file:
    python manage.py test assets.tests.test_time_choice_field
"""
from datetime import time

from django.test import SimpleTestCase

from assets.forms import TimeChoiceField


class TimeChoiceFieldTests(SimpleTestCase):
    def test_has_changed_does_not_crash_with_a_time_initial(self):
        field = TimeChoiceField()
        field.has_changed(time(9, 0), "540")  # no exception (9:00 = 540 min)

    def test_has_changed_is_false_when_the_submitted_value_matches_initial(self):
        field = TimeChoiceField()
        self.assertFalse(field.has_changed(time(9, 0), "540"))

    def test_has_changed_is_true_on_a_real_change(self):
        field = TimeChoiceField()
        self.assertTrue(field.has_changed(time(9, 0), "600"))

    def test_has_changed_handles_a_blank_initial(self):
        field = TimeChoiceField()
        self.assertTrue(field.has_changed(None, "540"))
        self.assertFalse(field.has_changed(None, ""))

    def test_coerce_passes_through_an_existing_time(self):
        value = time(14, 30)
        self.assertEqual(TimeChoiceField._coerce(value), value)

    def test_coerce_converts_submitted_minutes_string(self):
        self.assertEqual(TimeChoiceField._coerce("510"), time(8, 30))

    def test_coerce_treats_blank_as_none(self):
        self.assertIsNone(TimeChoiceField._coerce(""))
        self.assertIsNone(TimeChoiceField._coerce(None))

    def test_prepare_value_adds_an_out_of_preset_time_as_an_extra_choice(self):
        field = TimeChoiceField()
        # 05:00 (300 min) is before the 07:00 working-day start — not a preset.
        prepared = field.prepare_value(time(5, 0))
        self.assertEqual(prepared, 300)
        self.assertIn(300, dict(field.choices))

    def test_choices_cover_the_working_day_in_15_minute_steps(self):
        field = TimeChoiceField()
        labels = dict(field.choices)
        self.assertIn(7 * 60, labels)   # 07:00
        self.assertIn(17 * 60, labels)  # 17:00
        self.assertEqual(labels[9 * 60 + 15], "09:15")
