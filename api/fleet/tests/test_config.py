"""
api/fleet/tests/test_config.py

Tests 69-75: GET /en/api/config/?key=... configuration list endpoint

Run just this file:
    python manage.py test api.fleet.tests.test_config
"""

from django.test import TestCase
from rest_framework.test import APIClient

from company.models import Company, ConfigurationList
from api.fleet.tests.helpers import make_company, make_user, make_employee


class ConfigListTests(TestCase):
    """
    Tests for GET /en/api/config/?key=...
    These confirm the dropdown options the app uses are returned correctly.
    """

    def setUp(self):
        self.api_client = APIClient()
        self.company = make_company()
        self.user = make_user()
        self.employee = make_employee(self.company, self.user)
        self.api_client.force_authenticate(user=self.user)

    def _make_config(self, key, values):
        return ConfigurationList.objects.create(
            company=self.company,
            key=key,
            values=values,
        )

    def _get_config(self, key):
        return self.api_client.get(f"/en/api/config/?key={key}")

    # Test 69
    def test_config_returns_m3_options(self):
        """GET /config/?key=m3 must return the m3 values list."""
        self._make_config("m3", ["3", "6", "9", "12"])
        response = self._get_config("m3")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["values"], ["3", "6", "9", "12"])

    # Test 70
    def test_config_returns_materiales_options(self):
        """GET /config/?key=materiales must return the materiales list."""
        self._make_config("materiales", ["Hormigon", "Tierra", "Escombros"])
        response = self._get_config("materiales")
        self.assertEqual(response.json()["values"], ["Hormigon", "Tierra", "Escombros"])

    # Test 71
    def test_config_returns_material_vertido_options(self):
        """GET /config/?key=material_vertido must return the material_vertido list."""
        self._make_config("material_vertido", ["RCD Limpio", "RCD Mixto"])
        response = self._get_config("material_vertido")
        self.assertEqual(response.json()["values"], ["RCD Limpio", "RCD Mixto"])

    # Test 72
    def test_config_returns_trabajos_options(self):
        """GET /config/?key=trabajos must return the trabajos list."""
        self._make_config("trabajos", ["Recogida", "Entrega", "Verteo"])
        response = self._get_config("trabajos")
        self.assertEqual(response.json()["values"], ["Recogida", "Entrega", "Verteo"])

    # Test 73
    def test_config_returns_empty_list_for_unknown_key(self):
        """
        Requesting a key that doesn't exist must return an empty list —
        not a 404 or a crash.
        """
        response = self._get_config("nonexistent_key")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["values"], [])

    # Test 74
    def test_config_unauthenticated_is_rejected(self):
        """No token — config endpoint must return 401."""
        self.api_client.force_authenticate(user=None)
        response = self._get_config("m3")
        self.assertEqual(response.status_code, 401)

    # Test 75
    def test_config_is_company_isolated(self):
        """
        A config list belonging to company A must not be visible
        to a user from company B.
        """
        other_company = Company.objects.create(name="Other Company")
        ConfigurationList.objects.create(
            company=other_company,
            key="m3",
            values=["1", "2", "3"],
        )
        # Our user belongs to self.company which has no m3 config
        response = self._get_config("m3")
        self.assertEqual(response.json()["values"], [])
