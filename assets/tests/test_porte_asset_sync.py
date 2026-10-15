"""
Porte plannings keep `asset` for a specifically chosen cargo (cuba / machine
cargo historically). They no longer copy `transport_asset` onto `asset` —
GET /portes/ falls back with `p.asset or p.transport_asset`, and machine
portes use the separate `machine` field.
"""
from datetime import date

from django.test import TestCase

from api.fleet.tests.helpers import make_company, make_zone
from assets.constants import SERVICE_TYPE_MATERIAL_EXTERNO
from assets.models import AssetPlanning
from assets.tests.factories import make_truck, make_truck_type


class PorteAssetSyncTests(TestCase):
    def test_save_never_copies_transport_asset_onto_asset(self):
        company = make_company()
        truck_type = make_truck_type(company, name="Camión")
        truck = make_truck(company, name="T-1", asset_type=truck_type)
        planning = AssetPlanning.objects.create(
            company=company,
            asset=None,
            transport_asset=truck,
            origin=make_zone(company, name="Origin"),
            destination=make_zone(company, name="Destination"),
            scheduled_date=date.today(),
            planning_type="porte",
            service_type=SERVICE_TYPE_MATERIAL_EXTERNO,
            status="pending",
        )
        self.assertEqual(planning.transport_asset_id, truck.id)
        self.assertIsNone(planning.asset_id)

        # Nor on a partial save that only touches the truck.
        other_truck = make_truck(company, name="T-2", asset_type=truck_type)
        planning.transport_asset = other_truck
        planning.save(update_fields=["transport_asset"])
        planning.refresh_from_db()
        self.assertEqual(planning.transport_asset_id, other_truck.id)
        self.assertIsNone(planning.asset_id)
