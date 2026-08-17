"""
api/fleet/tests/test_first_transaction_relocation.py

Tests for the first-transaction geofence leniency + auto-relocate behavior:
a WorkingZone whose location has never been confirmed by a real transaction
(location_confirmed_at is None) is matched against a 2km radius instead of
the normal 250m, and the first transaction that lands within that radius
locks in the submitted GPS as the zone's location going forward.

Run just this file:
    python manage.py test api.fleet.tests.test_first_transaction_relocation
"""

import json
from unittest.mock import patch

from django.test import TestCase

from assets.models import AssetTransaction, TransactionTypeChoices
from api.fleet.tests.helpers import (
    make_company,
    make_user,
    make_employee,
    make_asset_type,
    make_asset,
    make_zone,
    make_planning,
)

ZONE_LAT = 40.416775
ZONE_LON = -3.703790
# ~1km north of the zone: within the 2km first-transaction radius,
# but outside the normal 250m radius.
NEARBY_LAT = ZONE_LAT + 0.009
# ~2.8km north of the zone: outside even the widened 2km radius.
FAR_LAT = ZONE_LAT + 0.025


class MovementFirstTransactionRelocationTests(TestCase):
    def _run_movement(self, asset, employee, user, latitude, longitude, zone_id=None):
        from api.fleet.views.assets import MovementStrategy

        planning = make_planning(asset.company, asset=asset, assigned_to=employee)
        data = {
            "transaction_type": "movement",
            "latitude": latitude,
            "longitude": longitude,
            "matricula": "1234ABC",
            "planning_id": planning.id,
        }
        if zone_id is not None:
            data["zone_id"] = zone_id
        strategy = MovementStrategy(data, asset, employee, user)
        with patch("api.fleet.views.assets.send_asset_transaction_email_task.delay"):
            return strategy.execute()

    def setUp(self):
        self.company = make_company()
        self.user = make_user()
        self.employee = make_employee(self.company, self.user)
        self.asset_type = make_asset_type(self.company)

    # Unconfirmed zone, GPS ~1km off: must succeed and relocate the zone.
    def test_unconfirmed_zone_within_2km_succeeds_and_relocates(self):
        zone = make_zone(self.company, lat=ZONE_LAT, lon=ZONE_LON, confirmed=False)
        asset = make_asset(self.company, self.asset_type, employee=self.employee, zone=zone)

        response = self._run_movement(asset, self.employee, self.user, NEARBY_LAT, ZONE_LON)
        body = json.loads(response.content)

        self.assertTrue(body.get("success"))
        zone.refresh_from_db()
        self.assertIsNotNone(zone.location_confirmed_at)
        new_lat, new_lon = zone.get_coordinates()
        self.assertAlmostEqual(new_lat, NEARBY_LAT, places=4)
        self.assertAlmostEqual(new_lon, ZONE_LON, places=4)

    # Unconfirmed zone, GPS ~2.8km off: still rejected even with the wider radius.
    def test_unconfirmed_zone_beyond_2km_still_fails(self):
        zone = make_zone(self.company, lat=ZONE_LAT, lon=ZONE_LON, confirmed=False)
        asset = make_asset(self.company, self.asset_type, employee=self.employee, zone=zone)

        response = self._run_movement(asset, self.employee, self.user, FAR_LAT, ZONE_LON)

        self.assertEqual(response.status_code, 400)
        zone.refresh_from_db()
        self.assertIsNone(zone.location_confirmed_at)
        self.assertEqual(AssetTransaction.objects.count(), 0)

    # Once confirmed, the zone reverts to the strict 250m radius.
    def test_confirmed_zone_rejects_1km_offset(self):
        zone = make_zone(self.company, lat=ZONE_LAT, lon=ZONE_LON, confirmed=True)
        asset = make_asset(self.company, self.asset_type, employee=self.employee, zone=zone)

        response = self._run_movement(asset, self.employee, self.user, NEARBY_LAT, ZONE_LON)

        self.assertEqual(response.status_code, 400)

    # A confirmed zone's location must not move even on a successful transaction.
    def test_confirmed_zone_location_unchanged_on_success(self):
        zone = make_zone(self.company, lat=ZONE_LAT, lon=ZONE_LON, confirmed=True)
        original_confirmed_at = zone.location_confirmed_at
        asset = make_asset(self.company, self.asset_type, employee=self.employee, zone=zone)

        response = self._run_movement(asset, self.employee, self.user, ZONE_LAT, ZONE_LON)
        body = json.loads(response.content)

        self.assertTrue(body.get("success"))
        zone.refresh_from_db()
        self.assertEqual(zone.location_confirmed_at, original_confirmed_at)
        lat, lon = zone.get_coordinates()
        self.assertAlmostEqual(lat, ZONE_LAT, places=4)
        self.assertAlmostEqual(lon, ZONE_LON, places=4)


class DeliveryFirstTransactionRelocationTests(TestCase):
    def _run_delivery(self, asset, employee, user, latitude, longitude, zone_id=None):
        from api.fleet.views.assets import DeliveryStrategy

        planning = make_planning(asset.company, asset=asset, assigned_to=employee)
        data = {
            "transaction_type": "delivery",
            "latitude": latitude,
            "longitude": longitude,
            "matricula": "1234ABC",
            "planning_id": planning.id,
        }
        if zone_id is not None:
            data["zone_id"] = zone_id
        strategy = DeliveryStrategy(data, asset, employee, user)
        with patch("api.fleet.views.assets.send_asset_transaction_email_task.delay"):
            return strategy.execute()

    def setUp(self):
        self.company = make_company()
        self.user = make_user()
        self.employee = make_employee(self.company, self.user)
        self.asset_type = make_asset_type(self.company)
        self.asset = make_asset(self.company, self.asset_type, employee=self.employee)

    # Auto-detected unconfirmed zone ~1km off: succeeds via the widened
    # fallback search and relocates the zone.
    def test_unconfirmed_zone_within_2km_succeeds_and_relocates(self):
        dest_zone = make_zone(self.company, name="Destino", lat=ZONE_LAT, lon=ZONE_LON, confirmed=False)

        response = self._run_delivery(self.asset, self.employee, self.user, NEARBY_LAT, ZONE_LON)
        body = json.loads(response.content)

        self.assertTrue(body.get("success"))
        dest_zone.refresh_from_db()
        self.assertIsNotNone(dest_zone.location_confirmed_at)
        self.asset.refresh_from_db()
        self.assertEqual(self.asset.current_working_zone, dest_zone)

    # Auto-detect fallback still fails when GPS is beyond 2km of the
    # unconfirmed zone.
    def test_unconfirmed_zone_beyond_2km_still_fails(self):
        dest_zone = make_zone(self.company, name="Destino", lat=ZONE_LAT, lon=ZONE_LON, confirmed=False)

        response = self._run_delivery(self.asset, self.employee, self.user, FAR_LAT, ZONE_LON)

        self.assertEqual(response.status_code, 400)
        dest_zone.refresh_from_db()
        self.assertIsNone(dest_zone.location_confirmed_at)
        self.assertEqual(AssetTransaction.objects.count(), 0)
