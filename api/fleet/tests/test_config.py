"""
api/fleet/tests/test_config.py

GET /en/api/config/?key=... configuration list endpoint

Run just this file:
    python manage.py test api.fleet.tests.test_config
"""

from django.test import TestCase

from company.models import Company, ConfigurationList
from api.fleet.tests.helpers import ApiFixtureMixin


class ConfigListTests(ApiFixtureMixin, TestCase):
    """
    Tests for GET /en/api/config/?key=...
    These confirm the dropdown options the app uses are returned correctly.
    """

    def setUp(self):
        super().setUp()

    def _make_config(self, key, values):
        return ConfigurationList.objects.create(
            company=self.company,
            key=key,
            values=values,
        )

    def _get_config(self, key):
        return self.api_client.get(f"/en/api/config/?key={key}")

    def test_config_returns_the_values_list_for_each_key(self):
        """GET /config/?key=<key> must return that key's values list."""
        lists = {
            "m3": ["3", "6", "9", "12"],
            "materiales": ["Hormigon", "Tierra", "Escombros"],
            "material_vertido": ["RCD Limpio", "RCD Mixto"],
            "trabajos": ["Recogida", "Entrega", "Verteo"],
        }
        for key, values in lists.items():
            self._make_config(key, values)
        for key, values in lists.items():
            with self.subTest(key=key):
                response = self._get_config(key)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["values"], values)

    def test_config_returns_empty_list_for_unknown_key(self):
        """
        Requesting a key that doesn't exist must return an empty list —
        not a 404 or a crash.
        """
        response = self._get_config("nonexistent_key")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["values"], [])

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
