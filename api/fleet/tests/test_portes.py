"""
api/fleet/tests/test_portes.py

Tests 46-50: Porte and cuba planning list endpoints

Run just this file:
    python manage.py test api.fleet.tests.test_portes
"""

from datetime import date

from django.test import TestCase
from rest_framework.test import APIClient

from assets.models import AssetPlanning
from api.fleet.tests.helpers import (
    make_company,
    make_user,
    make_employee,
    make_asset_type,
    make_asset,
    make_zone,
)


class PortePlanningTests(TestCase):
    """
    Tests for the porte and cuba planning list endpoints.
    Portes and cubas are separate planning types and must not leak into each other.
    """

    def setUp(self):
        self.api_client = APIClient()
        self.company = make_company()
        self.user = make_user()
        self.employee = make_employee(self.company, self.user)
        self.asset_type = make_asset_type(self.company)
        self.origin = make_zone(self.company, name="Origen")
        self.destination = make_zone(self.company, name="Destino")
        self.asset = make_asset(self.company, self.asset_type, employee=self.employee)
        self.api_client.force_authenticate(user=self.user)

    def _make_planning(self, planning_type="porte", status="pending"):
        return AssetPlanning.objects.create(
            company=self.company,
            asset=self.asset,
            origin=self.origin,
            destination=self.destination,
            assigned_to=self.employee,
            scheduled_date=date.today(),
            planning_type=planning_type,
            status=status,
        )

    # Test 46
    def test_porte_planning_appears_in_portes_list(self):
        """A porte planning must appear in GET /en/api/portes/"""
        self._make_planning(planning_type="porte")
        response = self.api_client.get("/en/api/portes/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()["transport_plannings"]), 1)

    # Test 47
    def test_cuba_planning_appears_in_plannings_list(self):
        """A cuba planning must appear in GET /en/api/plannings/"""
        self._make_planning(planning_type="cuba")
        response = self.api_client.get("/en/api/plannings/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()["plannings"]), 1)

    # Test 48
    def test_porte_planning_does_not_appear_in_cuba_list(self):
        """A porte planning must NOT appear in the cuba plannings list."""
        self._make_planning(planning_type="porte")
        response = self.api_client.get("/en/api/plannings/")
        self.assertEqual(len(response.json()["plannings"]), 0)

    # Test 49
    def test_cuba_planning_does_not_appear_in_portes_list(self):
        """A cuba planning must NOT appear in the portes list."""
        self._make_planning(planning_type="cuba")
        response = self.api_client.get("/en/api/portes/")
        self.assertEqual(len(response.json()["transport_plannings"]), 0)

    # Test 50
    def test_delivered_porte_does_not_appear_in_list(self):
        """
        A porte with status='delivered' must not appear in the active list.
        Only pending and in_transit plannings should show.
        """
        self._make_planning(planning_type="porte", status="delivered")
        response = self.api_client.get("/en/api/portes/")
        self.assertEqual(len(response.json()["transport_plannings"]), 0)
