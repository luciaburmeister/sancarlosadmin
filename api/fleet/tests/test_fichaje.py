"""
api/fleet/tests/test_fichaje.py

Clock-in and clock-out attendance endpoints

Run just this file:
    python manage.py test api.fleet.tests.test_fichaje
"""

import json

from django.test import TestCase

from company.models import WorkAttendance
from api.fleet.tests.helpers import ApiFixtureMixin


class FichajeTests(ApiFixtureMixin, TestCase):
    """Tests for the attendance clock-in and clock-out endpoints."""

    def setUp(self):
        super().setUp()

    def _clock_in(self, lat=40.416775, lon=-3.703790):
        return self.api_client.post(
            "/en/api/attendance/clock-in/",
            data=json.dumps({"latitude": lat, "longitude": lon}),
            content_type="application/json",
        )

    def _clock_out(self, lat=40.416775, lon=-3.703790):
        return self.api_client.post(
            "/en/api/attendance/clock-out/",
            data=json.dumps({"latitude": lat, "longitude": lon}),
            content_type="application/json",
        )

    def test_clock_in_creates_one_attendance_record_for_the_employee(self):
        """Clock-in must answer 'success': true and create exactly one
        WorkAttendance row linked to the employee who clocked in."""
        response = self._clock_in()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["success"])
        self.assertEqual(WorkAttendance.objects.count(), 1)
        self.assertEqual(WorkAttendance.objects.first().employee, self.employee)

    def test_cannot_clock_in_twice(self):
        """
        Trying to clock in when already clocked in must return an error.
        The second request must not create a second attendance row.
        """
        self._clock_in()
        response = self._clock_in()
        self.assertIn("error", response.json())
        self.assertEqual(WorkAttendance.objects.count(), 1)

    def test_clock_out_updates_existing_record(self):
        """
        Clocking out must update the existing record's clock_out_time —
        not create a new row.
        """
        self._clock_in()
        self._clock_out()
        self.assertEqual(WorkAttendance.objects.count(), 1)
        self.assertIsNotNone(WorkAttendance.objects.first().clock_out_time)

    def test_clock_out_without_clock_in_returns_error(self):
        """Clocking out without having clocked in first must return an error."""
        response = self._clock_out()
        self.assertIn("error", response.json())

    def test_attendance_status_is_false_when_not_clocked_in(self):
        """GET /attendance/status/ must return is_clocked_in: false when not clocked in."""
        response = self.api_client.get("/en/api/attendance/status/")
        self.assertFalse(response.json()["is_clocked_in"])

    def test_attendance_status_is_true_after_clock_in(self):
        """GET /attendance/status/ must return is_clocked_in: true after clocking in."""
        self._clock_in()
        response = self.api_client.get("/en/api/attendance/status/")
        self.assertTrue(response.json()["is_clocked_in"])
