"""
api/fleet/tests/test_fichaje.py

Tests 55-63: Clock-in and clock-out attendance endpoints

Run just this file:
    python manage.py test api.fleet.tests.test_fichaje
"""

import json

from django.test import TestCase
from rest_framework.test import APIClient

from company.models import WorkAttendance
from api.fleet.tests.helpers import make_company, make_user, make_employee


class FichajeTests(TestCase):
    """Tests for the attendance clock-in and clock-out endpoints."""

    def setUp(self):
        self.api_client = APIClient()
        self.company = make_company()
        self.user = make_user()
        self.employee = make_employee(self.company, self.user)
        self.api_client.force_authenticate(user=self.user)

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

    # Test 55
    def test_clock_in_creates_attendance_record(self):
        """Clocking in must create one WorkAttendance row in the database."""
        self._clock_in()
        self.assertEqual(WorkAttendance.objects.count(), 1)

    # Test 56
    def test_clock_in_returns_success(self):
        """Clock-in response must contain 'success': true."""
        response = self._clock_in()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["success"])

    # Test 57
    def test_clock_in_links_to_correct_employee(self):
        """The attendance record must be linked to the employee who clocked in."""
        self._clock_in()
        attendance = WorkAttendance.objects.first()
        self.assertEqual(attendance.employee, self.employee)

    # Test 58
    def test_cannot_clock_in_twice(self):
        """
        Trying to clock in when already clocked in must return an error.
        The second request must not create a second attendance row.
        """
        self._clock_in()
        response = self._clock_in()
        self.assertIn("error", response.json())
        self.assertEqual(WorkAttendance.objects.count(), 1)

    # Test 59
    def test_clock_out_updates_existing_record(self):
        """
        Clocking out must update the existing record's clock_out_time —
        not create a new row.
        """
        self._clock_in()
        self._clock_out()
        self.assertEqual(WorkAttendance.objects.count(), 1)
        self.assertIsNotNone(WorkAttendance.objects.first().clock_out_time)

    # Test 60
    def test_clock_out_without_clock_in_returns_error(self):
        """Clocking out without having clocked in first must return an error."""
        response = self._clock_out()
        self.assertIn("error", response.json())

    # Test 61
    def test_unauthenticated_clock_in_is_rejected(self):
        """No JWT token — clock-in must return 401."""
        self.api_client.force_authenticate(user=None)
        response = self._clock_in()
        self.assertEqual(response.status_code, 401)

    # Test 62
    def test_attendance_status_is_false_when_not_clocked_in(self):
        """GET /attendance/status/ must return is_clocked_in: false when not clocked in."""
        response = self.api_client.get("/en/api/attendance/status/")
        self.assertFalse(response.json()["is_clocked_in"])

    # Test 63
    def test_attendance_status_is_true_after_clock_in(self):
        """GET /attendance/status/ must return is_clocked_in: true after clocking in."""
        self._clock_in()
        response = self.api_client.get("/en/api/attendance/status/")
        self.assertTrue(response.json()["is_clocked_in"])
