"""
api/fleet/tests/test_portes.py

Tests 46-50: Porte and cuba planning list endpoints

Run just this file:
    python manage.py test api.fleet.tests.test_portes
"""

import base64
from datetime import date, time

from django.test import TestCase
from rest_framework.test import APIClient

from assets.models import Asset, AssetPlanning, AssetTypeCategory
from api.fleet.tests.helpers import (
    make_company,
    make_user,
    make_employee,
    make_asset_type,
    make_asset,
    make_zone,
)

# 1x1 transparent PNG, used wherever the submit endpoint needs a base64 image.
TINY_PNG_B64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="


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


class PorteWithoutAssetTests(TestCase):
    """
    Regression tests for service types with no cargo asset (Grúa, Material
    Externo) — `AssetPlanning.asset` is legitimately None for these since
    `asset` became optional (2026-08-04). Covers the two production bugs this
    caused: GET /portes/ 500ing for the whole driver (unguarded p.asset.id/
    .name), and POST /portes/<id>/submit/ 500ing on planning.asset.company —
    plus the truck-QR fallback and its same-truck-same-day dedup.
    """

    def setUp(self):
        self.api_client = APIClient()
        self.company = make_company()
        self.user = make_user()
        self.employee = make_employee(self.company, self.user)
        self.origin = make_zone(self.company, name="Origen")
        self.destination = make_zone(self.company, name="Destino")
        self.truck_type = make_asset_type(self.company, name="Camión")
        self.truck_type.category = AssetTypeCategory.TRUCK
        self.truck_type.save()
        self.truck = Asset.objects.create(
            company=self.company, asset_type=self.truck_type, name="MERCEDES 0709CBB",
        )
        self.api_client.force_authenticate(user=self.user)

    def _make_planning(self, *, transport_asset=None, scheduled_time=None, status="pending"):
        return AssetPlanning.objects.create(
            company=self.company,
            asset=None,
            transport_asset=transport_asset,
            origin=self.origin,
            destination=self.destination,
            assigned_to=self.employee,
            scheduled_date=date.today(),
            scheduled_time=scheduled_time,
            planning_type="porte",
            service_type="porte_material",
            status=status,
        )

    def test_list_does_not_500_when_asset_is_none(self):
        """The bug from the screenshots: GET /portes/ must not 500 when a planning has no asset."""
        self._make_planning(transport_asset=self.truck)
        response = self.api_client.get("/en/api/portes/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()["transport_plannings"]), 1)

    def test_list_falls_back_to_transport_asset_for_qr_scan(self):
        """No `asset` but a truck assigned: asset_id/asset_name must be the truck's, so scanning it still works."""
        self._make_planning(transport_asset=self.truck)
        response = self.api_client.get("/en/api/portes/")
        item = response.json()["transport_plannings"][0]
        self.assertEqual(item["asset_id"], self.truck.id)
        self.assertEqual(item["asset_name"], self.truck.name)

    def test_list_returns_zero_sentinel_when_no_asset_and_no_truck(self):
        """No `asset` and no truck either: asset_id/asset_name must be the safe sentinel (0/""), not a crash."""
        self._make_planning(transport_asset=None)
        response = self.api_client.get("/en/api/portes/")
        self.assertEqual(response.status_code, 200)
        item = response.json()["transport_plannings"][0]
        self.assertEqual(item["asset_id"], 0)
        self.assertEqual(item["asset_name"], "")

    def test_only_earliest_of_two_same_truck_plannings_is_scannable(self):
        """Two porte plannings sharing a truck the same day: only the earliest gets the truck's id — the other gets 0."""
        earlier = self._make_planning(transport_asset=self.truck, scheduled_time=time(8, 0))
        later = self._make_planning(transport_asset=self.truck, scheduled_time=time(13, 0))
        response = self.api_client.get("/en/api/portes/")
        by_id = {item["id"]: item for item in response.json()["transport_plannings"]}
        self.assertEqual(by_id[earlier.id]["asset_id"], self.truck.id)
        self.assertEqual(by_id[later.id]["asset_id"], 0)

    def test_submit_does_not_500_when_asset_is_none(self):
        """The bug from the driver's screenshot: submitting an albarán must not 500 on planning.asset.company."""
        from assets.models import AssetTransaction

        planning = self._make_planning(transport_asset=self.truck)
        response = self.api_client.post(
            f"/en/api/portes/{planning.id}/submit/",
            data={
                "action": "recogida",
                "firma": TINY_PNG_B64,
                "imagen": TINY_PNG_B64,
                "matricula": "1234ABC",
                "notes": "",
            },
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json().get("success"))
        planning.refresh_from_db()
        self.assertEqual(planning.status, "in_transit")
        tx = AssetTransaction.objects.get(data__planning_id=planning.id)
        self.assertEqual(tx.asset_id, self.truck.id)
        # The new FK is populated alongside the existing soft data["planning_id"].
        self.assertEqual(tx.planning_id, planning.id)

    def test_submit_uses_transport_even_when_cargo_was_selected(self):
        """Temporary: porte save forces asset=transport_asset, so the transaction uses the truck."""
        from assets.models import AssetTransaction

        cargo_type = make_asset_type(self.company, name="Máquina")
        cargo = make_asset(self.company, cargo_type, employee=self.employee)
        planning = AssetPlanning.objects.create(
            company=self.company,
            asset=cargo,
            transport_asset=self.truck,
            origin=self.origin,
            destination=self.destination,
            assigned_to=self.employee,
            scheduled_date=date.today(),
            planning_type="porte",
            service_type="porte_maquina",
            status="pending",
        )
        planning.refresh_from_db()
        self.assertEqual(planning.asset_id, self.truck.id)
        response = self.api_client.post(
            f"/en/api/portes/{planning.id}/submit/",
            data={
                "action": "recogida",
                "firma": TINY_PNG_B64,
                "imagen": TINY_PNG_B64,
                "matricula": "1234ABC",
                "notes": "",
            },
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        tx = AssetTransaction.objects.get(data__planning_id=planning.id)
        self.assertEqual(tx.asset_id, self.truck.id)
