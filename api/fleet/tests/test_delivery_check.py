"""
GET /en/api/assets/<id>/delivery-check/?action=dejar

A full cuba (verteo still owed) must never be offered at the plan's destination
obra — even when the driver is standing on it, and even when a nearby almacén
makes the global can_deliver flag true. DeliveryStrategy already rejects that
drop; this endpoint is what the app uses to build the zone picker.
"""

from django.utils import timezone
from rest_framework.test import APIClient, APITestCase

from api.fleet.tests.helpers import (
    make_asset,
    make_asset_type,
    make_company,
    make_employee,
    make_planning,
    make_user,
    make_zone,
)
from assets.models import (
    AssetTransaction,
    CubaFillState,
    CubaOperationType,
    PlanningType,
    TransactionTypeChoices,
)
from company.models import WorkingZone


GPS_LAT = 40.416775
GPS_LON = -3.703790


class DeliveryCheckDejarTests(APITestCase):
    def setUp(self):
        self.client = APIClient()
        self.company = make_company()
        self.user = make_user()
        self.employee = make_employee(self.company, self.user)
        self.asset_type = make_asset_type(self.company)
        self.obra = make_zone(self.company, name="Obra Destino", lat=GPS_LAT, lon=GPS_LON)
        self.obra.zone_type = WorkingZone.ZoneType.OBRA
        self.obra.save()
        self.asset = make_asset(
            self.company, self.asset_type, employee=self.employee, zone=self.obra,
        )
        self.planning = make_planning(
            self.company, asset=self.asset, assigned_to=self.employee,
            planning_type=PlanningType.CUBA, cuba_operation_type=CubaOperationType.PUESTA,
            destination=self.obra,
        )
        self.client.force_authenticate(user=self.user)

    def _url(self, **extra):
        params = {
            "lat": GPS_LAT,
            "lon": GPS_LON,
            "action": "dejar",
            "planning_id": self.planning.id,
            **extra,
        }
        query = "&".join(f"{k}={v}" for k, v in params.items())
        return f"/en/api/assets/{self.asset.id}/delivery-check/?{query}"

    def _pick_up(self, *, full):
        data = {"cubaEstado": CubaFillState.FULL if full else CubaFillState.EMPTY}
        if full:
            data["vasAVerter"] = "Sí"
        AssetTransaction.objects.create(
            company=self.company, asset=self.asset, from_zone=self.obra,
            transaction_type=TransactionTypeChoices.MOVEMENT, date=timezone.now(),
            performed_by=self.employee, planning=self.planning, data=data,
        )

    def _almacen_nearby(self):
        almacen = make_zone(self.company, name="Almacen Cercano", lat=GPS_LAT, lon=GPS_LON)
        almacen.zone_type = WorkingZone.ZoneType.ALMACEN
        almacen.save()
        return almacen

    def test_empty_cuba_offers_destination_obra(self):
        self._pick_up(full=False)
        body = self.client.get(self._url()).json()
        zone_ids = [z["id"] for z in body["zones"]]
        self.assertIn(self.obra.id, zone_ids)
        dest = next(z for z in body["zones"] if z["id"] == self.obra.id)
        self.assertTrue(dest["can_deliver"])
        self.assertTrue(dest["is_planning_destination"])
        self.assertFalse(body["destination_blocked_full"])
        self.assertFalse(body["verteo_required"])

    def test_full_cuba_omits_destination_obra_even_with_nearby_warehouse(self):
        """The bug: global can_deliver became true because of the almacén, so
        the app let the driver pick the dest obra and fill a dejada as empty."""
        almacen = self._almacen_nearby()
        self._pick_up(full=True)
        body = self.client.get(self._url()).json()
        zone_ids = [z["id"] for z in body["zones"]]
        self.assertNotIn(self.obra.id, zone_ids)
        self.assertIn(almacen.id, zone_ids)
        self.assertTrue(body["destination_blocked_full"])
        self.assertEqual(body["destination_zone_name"], str(self.obra))
        self.assertTrue(body["verteo_required"])
        self.assertTrue(body["can_deliver"])

    def test_full_cuba_near_dest_with_no_warehouse_does_not_offer_obra(self):
        self._pick_up(full=True)
        body = self.client.get(self._url()).json()
        self.assertEqual(body["zones"], [])
        self.assertTrue(body["destination_blocked_full"])
        self.assertTrue(body["verteo_required"])
        self.assertFalse(body["can_deliver"])
        self.assertFalse(body.get("at_vertedero", False))

    def test_full_cuba_can_still_be_offered_at_an_almacen_destination(self):
        almacen = self._almacen_nearby()
        self.planning.destination = almacen
        self.planning.save(update_fields=["destination"])
        self._pick_up(full=True)
        body = self.client.get(self._url()).json()
        zone_ids = [z["id"] for z in body["zones"]]
        self.assertIn(almacen.id, zone_ids)
        self.assertFalse(body["destination_blocked_full"])
        dest = next(z for z in body["zones"] if z["id"] == almacen.id)
        self.assertTrue(dest["can_deliver"])
        self.assertTrue(dest["is_planning_destination"])
