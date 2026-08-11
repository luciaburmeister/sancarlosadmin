"""
api/fleet/tests/test_strategies.py

Tests 9-25: MovementStrategy, DeliveryStrategy, ReceiptStrategy

Run just this file:
    python manage.py test api.fleet.tests.test_strategies
"""

import json
from unittest.mock import patch

from django.test import TestCase

from datetime import date

from assets.models import (
    AssetPlanning,
    AssetTransaction,
    AssetTransactionReceipt,
    PlanningStatus,
    PlanningType,
    TransactionTypeChoices,
)
from api.fleet.tests.helpers import (
    make_company,
    make_user,
    make_employee,
    make_asset_type,
    make_asset,
    make_zone,
)


class MovementStrategyTests(TestCase):
    """
    Tests for MovementStrategy — the code that runs when a driver
    does a recogida (picks up a cuba).
    """

    def setUp(self):
        self.company = make_company()
        self.user = make_user()
        self.employee = make_employee(self.company, self.user)
        self.asset_type = make_asset_type(self.company)
        self.zone = make_zone(self.company, lat=40.416775, lon=-3.703790)
        self.asset = make_asset(
            self.company, self.asset_type,
            employee=self.employee,
            zone=self.zone,
        )

    def _run_movement(self, extra_data=None):
        """Helper that runs MovementStrategy directly with a valid payload."""
        from api.fleet.views.assets import MovementStrategy

        data = {
            "transaction_type": "movement",
            "latitude": 40.416775,
            "longitude": -3.703790,
            "matricula": "1234ABC",
            "tipo_camion": "Volquete",
            "implemento": "Gancho",
            "cliente": "Cliente Test",
            "obra": "Obra Test",
            "trabajo": "Recogida",
            "tipo_cuba": "6m3",
            "m3": "6",
            "vertedero": "Vertedero Norte",
            "zone_id": self.zone.id,
        }
        if extra_data:
            data.update(extra_data)

        strategy = MovementStrategy(data, self.asset, self.employee, self.user)
        with patch("api.fleet.views.assets.send_asset_transaction_email_task.delay"):
            return strategy.execute()

    # Test 9
    def test_movement_strategy_saves_transaction(self):
        """After execute() runs, exactly one AssetTransaction row must exist."""
        self._run_movement()
        self.assertEqual(AssetTransaction.objects.count(), 1)

    # Test 10
    def test_movement_strategy_does_not_crash(self):
        """
        execute() must not raise any exception.
        This is the test that would have caught the NameError production incident.
        """
        try:
            self._run_movement()
        except Exception as e:
            self.fail(f"MovementStrategy.execute() raised an exception: {e}")

    # Test 11
    def test_movement_strategy_transaction_type_is_correct(self):
        """The saved transaction must have transaction_type = MOVEMENT."""
        self._run_movement()
        tx = AssetTransaction.objects.first()
        self.assertEqual(tx.transaction_type, TransactionTypeChoices.MOVEMENT)

    # Test 12
    def test_movement_albaran_fields_save_to_database(self):
        """
        The albaran fields sent by the app must actually end up saved
        in the transaction's data JSON field.
        """
        self._run_movement()
        tx = AssetTransaction.objects.first()
        self.assertEqual(tx.data.get("matricula"), "1234ABC")
        self.assertEqual(tx.data.get("tipo_camion"), "Volquete")
        self.assertEqual(tx.data.get("implemento"), "Gancho")
        self.assertEqual(tx.data.get("cliente"), "")  # filled from zone.client; test zone has no client
        self.assertEqual(tx.data.get("obra"), "Obra Test")
        self.assertEqual(tx.data.get("trabajo"), "Recogida")
        self.assertEqual(tx.data.get("tipo_cuba"), "6m3")
        self.assertEqual(tx.data.get("m3"), "6")
        self.assertEqual(tx.data.get("vertedero"), "Vertedero Norte")

    # Test 13
    def test_movement_response_contains_success_and_transaction_id(self):
        """
        The response JSON must contain 'success': True and 'transaction_id'.
        The Android app reads both of these fields.
        """
        response = self._run_movement()
        body = json.loads(response.content)
        self.assertTrue(body["success"])
        self.assertIn("transaction_id", body)

    def test_movement_without_planning_id_leaves_fk_blank(self):
        """Backward compatibility: the current app never sends planning_id for cuba."""
        self._run_movement()
        tx = AssetTransaction.objects.first()
        self.assertIsNone(tx.planning_id)

    def test_movement_with_planning_id_links_fk_and_syncs_status(self):
        planning = AssetPlanning.objects.create(
            company=self.company, asset=self.asset, assigned_to=self.employee,
            scheduled_date=date.today(), planning_type=PlanningType.CUBA, status=PlanningStatus.PENDING,
        )
        self._run_movement(extra_data={"planning_id": planning.id})
        tx = AssetTransaction.objects.first()
        self.assertEqual(tx.planning_id, planning.id)
        planning.refresh_from_db()
        self.assertEqual(planning.status, PlanningStatus.IN_TRANSIT)


class DeliveryStrategyTests(TestCase):
    """Tests for DeliveryStrategy — when a driver delivers (deja) a cuba."""

    def setUp(self):
        self.company = make_company()
        self.user = make_user()
        self.employee = make_employee(self.company, self.user)
        self.asset_type = make_asset_type(self.company)
        self.origin_zone = make_zone(self.company, name="Origen", lat=40.41, lon=-3.70)
        self.dest_zone = make_zone(self.company, name="Destino", lat=40.42, lon=-3.71)
        self.asset = make_asset(
            self.company, self.asset_type,
            employee=self.employee,
            zone=self.origin_zone,
        )

    def _run_delivery(self, extra_data=None):
        from api.fleet.views.assets import DeliveryStrategy

        data = {
            "transaction_type": "delivery",
            "latitude": 40.42,
            "longitude": -3.71,
            "matricula": "1234ABC",
            "tipo_camion": "Volquete",
            "implemento": "Gancho",
            "cliente": "Cliente Test",
            "obra": "Obra Test",
            "trabajo": "Entrega",
            "tipo_cuba": "6m3",
            "m3": "6",
            "vertedero": "",
            "zone_id": self.dest_zone.id,
        }
        if extra_data:
            data.update(extra_data)
        strategy = DeliveryStrategy(data, self.asset, self.employee, self.user)
        with patch("api.fleet.views.assets.send_asset_transaction_email_task.delay"):
            return strategy.execute()

    # Test 14
    def test_delivery_strategy_saves_transaction(self):
        """After a delivery, one AssetTransaction must exist in the database."""
        self._run_delivery()
        self.assertEqual(AssetTransaction.objects.count(), 1)

    # Test 15
    def test_delivery_strategy_transaction_type_is_correct(self):
        """The saved transaction must have transaction_type = DELIVERY."""
        self._run_delivery()
        tx = AssetTransaction.objects.first()
        self.assertEqual(tx.transaction_type, TransactionTypeChoices.DELIVERY)

    # Test 16
    def test_delivery_albaran_fields_save_to_database(self):
        """Albaran fields must be saved in the transaction data JSON field."""
        self._run_delivery()
        tx = AssetTransaction.objects.first()
        self.assertEqual(tx.data.get("matricula"), "1234ABC")
        self.assertEqual(tx.data.get("tipo_camion"), "Volquete")
        self.assertEqual(tx.data.get("tipo_cuba"), "6m3")

    # Test 17
    def test_delivery_updates_asset_zone(self):
        """
        After a delivery, the asset's current_working_zone must be updated
        to the destination zone.
        """
        self._run_delivery()
        self.asset.refresh_from_db()
        self.assertEqual(self.asset.current_working_zone, self.dest_zone)

    def test_delivery_without_planning_id_leaves_fk_blank(self):
        self._run_delivery()
        tx = AssetTransaction.objects.first()
        self.assertIsNone(tx.planning_id)

    def test_delivery_with_planning_id_links_fk_and_syncs_status(self):
        planning = AssetPlanning.objects.create(
            company=self.company, asset=self.asset, assigned_to=self.employee,
            scheduled_date=date.today(), planning_type=PlanningType.CUBA, status=PlanningStatus.IN_TRANSIT,
        )
        self._run_delivery(extra_data={"planning_id": planning.id})
        tx = AssetTransaction.objects.first()
        self.assertEqual(tx.planning_id, planning.id)
        planning.refresh_from_db()
        self.assertEqual(planning.status, PlanningStatus.DELIVERED)


class ReceiptStrategyTests(TestCase):
    """Tests for ReceiptStrategy — when a driver registers a verteo."""

    def setUp(self):
        self.company = make_company()
        self.user = make_user()
        self.employee = make_employee(self.company, self.user)
        self.asset_type = make_asset_type(self.company)
        self.zone = make_zone(self.company)
        self.asset = make_asset(
            self.company, self.asset_type,
            employee=self.employee,
            zone=self.zone,
        )
        self.movement_tx = AssetTransaction.objects.create(
            asset=self.asset,
            company=self.company,
            transaction_type=TransactionTypeChoices.MOVEMENT,
            from_zone=self.zone,
            performed_by=self.employee,
        )

    def _run_receipt(self, extra_data=None):
        from api.fleet.views.assets import ReceiptStrategy

        data = {
            "transaction_type": "receipt",
            "nro_albaran": "ALB-001",
            "foto_albaran": "",
            "peso_neto": "500",
            "material_vertido": "RCD Limpio",
            "latitude": 40.416775,
            "longitude": -3.703790,
            "zone_id": self.zone.id,
        }
        if extra_data:
            data.update(extra_data)

        strategy = ReceiptStrategy(data, self.asset, self.employee, self.user)
        return strategy.execute()

    # Test 18
    def test_receipt_strategy_saves_transaction(self):
        """After a verteo, one AssetTransaction with type RECEIPT must exist."""
        self._run_receipt()
        self.assertTrue(
            AssetTransaction.objects.filter(
                transaction_type=TransactionTypeChoices.RECEIPT
            ).exists()
        )

    # Test 19
    def test_receipt_strategy_saves_receipt_row(self):
        """A corresponding AssetTransactionReceipt row must also be created."""
        self._run_receipt()
        self.assertEqual(AssetTransactionReceipt.objects.count(), 1)

    # Test 20
    def test_receipt_strategy_links_receipt_to_transaction(self):
        """The receipt's FK must point to the correct AssetTransaction."""
        self._run_receipt()
        tx = AssetTransaction.objects.filter(
            transaction_type=TransactionTypeChoices.RECEIPT
        ).first()
        receipt = AssetTransactionReceipt.objects.first()
        self.assertEqual(receipt.transaction, tx)

    # Test 21
    def test_receipt_strategy_saves_nro_albaran(self):
        """nro_albaran must be saved directly on the receipt row."""
        self._run_receipt()
        receipt = AssetTransactionReceipt.objects.first()
        self.assertEqual(receipt.nro_albaran, "ALB-001")

    # Test 22
    def test_receipt_strategy_saves_peso_neto(self):
        """peso_neto must be saved inside the receipt's data JSON field."""
        self._run_receipt()
        receipt = AssetTransactionReceipt.objects.first()
        self.assertEqual(receipt.data.get("peso_neto"), "500")

    # Test 23
    def test_receipt_strategy_saves_material_vertido(self):
        """material_vertido must be saved inside the receipt's data JSON field."""
        self._run_receipt()
        receipt = AssetTransactionReceipt.objects.first()
        self.assertEqual(receipt.data.get("material_vertido"), "RCD Limpio")

    # Test 24
    def test_receipt_strategy_links_to_original_movement(self):
        """
        The receipt must be linked to the original movement transaction
        via movement_transaction FK.
        """
        self._run_receipt()
        receipt = AssetTransactionReceipt.objects.first()
        self.assertEqual(receipt.movement_transaction, self.movement_tx)

    # Test 25
    def test_receipt_requires_nro_albaran(self):
        """
        nro_albaran is required for a verteo.
        Sending an empty string must return an error response.
        """
        response = self._run_receipt(extra_data={"nro_albaran": ""})
        body = json.loads(response.content)
        self.assertIn("error", body)
        self.assertEqual(response.status_code, 400)

    def test_receipt_with_planning_id_links_fk_but_does_not_touch_status(self):
        """Verteo is a side event — it links to the plan but never changes its status."""
        planning = AssetPlanning.objects.create(
            company=self.company, asset=self.asset, assigned_to=self.employee,
            scheduled_date=date.today(), planning_type=PlanningType.CUBA, status=PlanningStatus.IN_TRANSIT,
        )
        self._run_receipt(extra_data={"planning_id": planning.id})
        tx = AssetTransaction.objects.filter(transaction_type=TransactionTypeChoices.RECEIPT).first()
        self.assertEqual(tx.planning_id, planning.id)
        planning.refresh_from_db()
        self.assertEqual(planning.status, PlanningStatus.IN_TRANSIT)
