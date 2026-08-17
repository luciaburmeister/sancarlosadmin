"""
AssetPlanning of porte service types falls back to the truck on `asset`
only when no specific asset was chosen; a chosen `asset` is preserved.
"""
from datetime import date

from django.test import TestCase

from api.fleet.tests.helpers import make_asset, make_asset_type, make_company, make_zone
from assets.constants import (
    SERVICE_TYPE_CUBA,
    SERVICE_TYPE_GRUA,
    SERVICE_TYPE_MAQUINA_PROPIA,
    SERVICE_TYPE_MATERIAL_EXTERNO,
)
from assets.models import Asset, AssetPlanning, AssetType, AssetTypeCategory


class PorteAssetSyncTests(TestCase):
    def setUp(self):
        self.company = make_company()
        self.origin = make_zone(self.company, name="Origin")
        self.destination = make_zone(self.company, name="Destination")
        machine_type = make_asset_type(self.company, name="Máquina")
        self.machine = make_asset(self.company, machine_type, name="M-1")
        truck_type = AssetType.objects.create(
            company=self.company, name="Camión", category=AssetTypeCategory.TRUCK,
        )
        self.truck = Asset.objects.create(
            company=self.company, asset_type=truck_type, name="T-1",
        )

    def _create(self, *, service_type, planning_type, asset=None, transport_asset=None):
        return AssetPlanning.objects.create(
            company=self.company,
            asset=asset,
            transport_asset=transport_asset,
            origin=self.origin,
            destination=self.destination,
            scheduled_date=date.today(),
            planning_type=planning_type,
            service_type=service_type,
            status="pending",
        )

    def test_porte_maquina_keeps_selected_asset(self):
        planning = self._create(
            service_type=SERVICE_TYPE_MAQUINA_PROPIA,
            planning_type="porte",
            asset=self.machine,
            transport_asset=self.truck,
        )
        self.assertEqual(planning.asset_id, self.machine.id)

    def test_porte_material_sets_asset_from_transport(self):
        planning = self._create(
            service_type=SERVICE_TYPE_MATERIAL_EXTERNO,
            planning_type="porte",
            asset=None,
            transport_asset=self.truck,
        )
        self.assertEqual(planning.asset_id, self.truck.id)

    def test_grua_sets_asset_from_transport(self):
        planning = self._create(
            service_type=SERVICE_TYPE_GRUA,
            planning_type="porte",
            asset=None,
            transport_asset=self.truck,
        )
        self.assertEqual(planning.asset_id, self.truck.id)

    def test_cuba_keeps_selected_asset(self):
        cuba = make_asset(self.company, make_asset_type(self.company, "Cuba"), name="C-1")
        planning = self._create(
            service_type=SERVICE_TYPE_CUBA,
            planning_type="cuba",
            asset=cuba,
            transport_asset=self.truck,
        )
        self.assertEqual(planning.asset_id, cuba.id)

    def test_update_fields_transport_also_persists_asset(self):
        planning = self._create(
            service_type=SERVICE_TYPE_MATERIAL_EXTERNO,
            planning_type="porte",
            asset=None,
            transport_asset=None,
        )
        self.assertIsNone(planning.asset_id)
        planning.transport_asset = self.truck
        planning.save(update_fields=["transport_asset"])
        planning.refresh_from_db()
        self.assertEqual(planning.asset_id, self.truck.id)
