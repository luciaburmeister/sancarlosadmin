"""
api/fleet/tests/test_employees.py

Tests for GET /es/api/employees/ — active employees for the Laborer Movement picker.

Run just this file:
    python manage.py test api.fleet.tests.test_employees
"""
from datetime import date

from django.test import TestCase

from company.models import Employee
from api.fleet.tests.helpers import ApiFixtureMixin, make_company


class EmployeesListEndpointTests(ApiFixtureMixin, TestCase):
    def setUp(self):
        super().setUp()

    def _url(self):
        return "/es/api/employees/"

    def test_returns_active_company_employees(self):
        Employee.objects.create(
            company=self.company, first_name="Juan", last_name="Perez", hired_at=date.today(),
        )
        response = self.api_client.get(self._url())
        self.assertEqual(response.status_code, 200)
        names = [r["name"] for r in response.json()["results"]]
        self.assertIn("Juan Perez", names)
        self.assertIn("Test Driver", names)  # self.employee

    def test_excludes_inactive_employees(self):
        Employee.objects.create(
            company=self.company, first_name="Retired", last_name="Guy",
            hired_at=date.today(), is_active=False,
        )
        response = self.api_client.get(self._url())
        names = [r["name"] for r in response.json()["results"]]
        self.assertNotIn("Retired Guy", names)

    def test_excludes_other_companies_employees(self):
        other_company = make_company(name="Other Company")
        Employee.objects.create(
            company=other_company, first_name="Other", last_name="Co", hired_at=date.today(),
        )
        response = self.api_client.get(self._url())
        names = [r["name"] for r in response.json()["results"]]
        self.assertNotIn("Other Co", names)
