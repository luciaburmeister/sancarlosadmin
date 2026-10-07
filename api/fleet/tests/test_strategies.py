"""
api/fleet/tests/test_strategies.py

Tests 9-25: MovementStrategy, DeliveryStrategy, ReceiptStrategy

Run just this file:
    python manage.py test api.fleet.tests.test_strategies
"""

import json
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from assets.models import (
    Asset,
    AssetCurrentStatus,
    AssetTransaction,
    AssetTransactionReceipt,
    CubaFillState,
    CubaOperationType,
    CubaTransactionKey,
    PlanningStatus,
    PlanningType,
    PorteAction,
    PorteMovementKind,
    PorteTransactionDataKey,
    TransactionTypeChoices,
)
from api.fleet.tests.helpers import (
    make_company,
    make_user,
    make_employee,
    make_asset_type,
    make_asset,
    make_zone,
    make_planning,
)
from company.models import WorkingZone


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
        self.planning = make_planning(
            self.company, asset=self.asset, assigned_to=self.employee,
            cuba_operation_type=CubaOperationType.RETIRADA,
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
            "planning_id": self.planning.id,
        }
        if extra_data:
            data.update(extra_data)

        strategy = MovementStrategy(data, self.asset, self.employee, self.user)
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

    # Reference number is a plain pass-through field (not stripped by
    # MovementStrategy's form_data exclusion list), so Android sending it is
    # all that's needed -- no server-side wiring for this specific type.
    def test_movement_reference_number_passes_through_to_data(self):
        self._run_movement(extra_data={"reference_number": "REF-001"})
        tx = AssetTransaction.objects.first()
        self.assertEqual(tx.data.get("reference_number"), "REF-001")

    def test_movement_without_planning_id_is_rejected(self):
        """Cuba can't operate without a plan — strict mode."""
        response = self._run_movement(extra_data={"planning_id": None})
        self.assertEqual(response.status_code, 400)
        self.assertIn("error", json.loads(response.content))
        self.assertEqual(AssetTransaction.objects.count(), 0)

    def test_movement_with_someone_elses_planning_id_is_rejected(self):
        other_user = make_user(username="otherdriver")
        other_employee = make_employee(self.company, other_user)
        other_planning = make_planning(self.company, asset=self.asset, assigned_to=other_employee)
        response = self._run_movement(extra_data={"planning_id": other_planning.id})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(AssetTransaction.objects.count(), 0)

    def test_movement_with_a_porte_planning_id_is_rejected(self):
        porte_planning = make_planning(
            self.company, asset=self.asset, assigned_to=self.employee, planning_type=PlanningType.PORTE,
        )
        response = self._run_movement(extra_data={"planning_id": porte_planning.id})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(AssetTransaction.objects.count(), 0)

    def test_movement_with_planning_id_links_fk_and_closes_plan_when_not_full(self):
        """Retirada: an empty pickup is the whole job and closes the plan."""
        self._run_movement()
        tx = AssetTransaction.objects.first()
        self.assertEqual(tx.planning_id, self.planning.id)
        self.planning.refresh_from_db()
        self.assertEqual(self.planning.status, PlanningStatus.DELIVERED)

    def test_movement_with_full_cuba_leaves_plan_in_transit(self):
        """A full recogida leaves a retirada open until the verteo."""
        self._run_movement(extra_data={"cubaEstado": CubaFillState.FULL})
        self.planning.refresh_from_db()
        self.assertEqual(self.planning.status, PlanningStatus.IN_TRANSIT)

    def test_movement_against_a_puesta_planning_never_closes_it(self):
        """Loading the cuba for a puesta must not close it — only the drop-off does."""
        self.planning.cuba_operation_type = CubaOperationType.PUESTA
        self.planning.save(update_fields=["cuba_operation_type"])
        self._run_movement()
        self.planning.refresh_from_db()
        self.assertEqual(self.planning.status, PlanningStatus.IN_TRANSIT)


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
        self.planning = make_planning(
            self.company, asset=self.asset, assigned_to=self.employee,
            cuba_operation_type=CubaOperationType.PUESTA,
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
            "planning_id": self.planning.id,
        }
        if extra_data:
            data.update(extra_data)
        strategy = DeliveryStrategy(data, self.asset, self.employee, self.user)
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

    def test_delivery_reference_number_passes_through_to_data(self):
        self._run_delivery(extra_data={"reference_number": "REF-002"})
        tx = AssetTransaction.objects.first()
        self.assertEqual(tx.data.get("reference_number"), "REF-002")

    def test_delivery_without_planning_id_is_rejected(self):
        response = self._run_delivery(extra_data={"planning_id": None})
        self.assertEqual(response.status_code, 400)
        self.assertIn("error", json.loads(response.content))
        self.assertEqual(AssetTransaction.objects.count(), 0)

    def test_delivery_after_porte_with_no_next_cuba_plan(self):
        """Finished porte → pending_dropoff still points at that porte id.
        Leaving the cuba must work with no next CUBA plan (don't 400)."""
        from company.models import WorkingZone

        almacen = make_zone(self.company, name="Almacen", lat=40.42, lon=-3.71)
        almacen.zone_type = WorkingZone.ZoneType.ALMACEN
        almacen.save(update_fields=["zone_type"])
        porte = make_planning(
            self.company,
            asset=self.asset,
            assigned_to=self.employee,
            planning_type=PlanningType.PORTE,
            status=PlanningStatus.DELIVERED,
            service_type="porte_material",
        )
        AssetTransaction.objects.create(
            asset=self.asset,
            company=self.company,
            from_zone=self.origin_zone,
            transaction_type=TransactionTypeChoices.PORTE_MOVEMENT,
            date=timezone.now(),
            performed_by=self.employee,
            planning=porte,
            data={
                PorteTransactionDataKey.MOVEMENT_TYPE: PorteMovementKind.PORTE,
                PorteTransactionDataKey.ACTION: PorteAction.RECOGIDA,
            },
        )
        AssetTransaction.objects.create(
            asset=self.asset,
            company=self.company,
            from_zone=self.origin_zone,
            to_zone=self.dest_zone,
            transaction_type=TransactionTypeChoices.PORTE_DELIVERY,
            date=timezone.now(),
            performed_by=self.employee,
            planning=porte,
            data={
                PorteTransactionDataKey.MOVEMENT_TYPE: PorteMovementKind.PORTE,
                PorteTransactionDataKey.ACTION: PorteAction.ENTREGA,
            },
        )
        response = self._run_delivery(
            extra_data={
                "planning_id": porte.id,
                "zone_id": almacen.id,
                CubaTransactionKey.ESTADO: CubaFillState.EMPTY,
            },
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(
            AssetTransaction.objects.filter(
                transaction_type=TransactionTypeChoices.DELIVERY,
            ).count(),
            1,
        )
        self.asset.refresh_from_db()
        self.assertEqual(self.asset.current_working_zone_id, almacen.id)

    def test_delivery_with_planning_id_links_fk_and_syncs_status(self):
        self.planning.status = PlanningStatus.IN_TRANSIT
        self.planning.save(update_fields=["status"])
        self._run_delivery()
        tx = AssetTransaction.objects.first()
        self.assertEqual(tx.planning_id, self.planning.id)
        self.planning.refresh_from_db()
        self.assertEqual(self.planning.status, PlanningStatus.DELIVERED)

    def test_delivery_rejects_full_cuba_at_an_obra(self):
        """dest_zone defaults to zone_type=OBRA — never let a full cuba be
        dropped there, no matter the plan/operation type."""
        response = self._run_delivery(extra_data={"cubaEstado": "Cuba llena"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("error", json.loads(response.content))
        self.assertEqual(AssetTransaction.objects.count(), 0)

    def test_delivery_rejects_pending_verteo_at_an_obra_even_if_marked_empty(self):
        """A driver can't dodge the block by mismarking cubaEstado at the
        dejada — the server checks the asset's own pending-verteo state too."""
        AssetTransaction.objects.create(
            asset=self.asset, company=self.company, from_zone=self.origin_zone,
            transaction_type=TransactionTypeChoices.MOVEMENT, performed_by=self.employee,
            planning=self.planning, data={"cubaEstado": "Cuba llena"},
        )
        response = self._run_delivery(extra_data={"cubaEstado": "Cuba vacía"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(AssetTransaction.objects.filter(transaction_type=TransactionTypeChoices.DELIVERY).count(), 0)

    def test_delivery_allows_full_cuba_at_an_almacen(self):
        almacen = make_zone(self.company, name="Almacen", lat=40.42, lon=-3.71)
        almacen.zone_type = WorkingZone.ZoneType.ALMACEN
        almacen.save()
        response = self._run_delivery(extra_data={"cubaEstado": "Cuba llena", "zone_id": almacen.id})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(AssetTransaction.objects.filter(transaction_type=TransactionTypeChoices.DELIVERY).exists())

    def test_delivery_rejects_empty_cuba_at_an_almacen_when_verteo_pending(self):
        """A cuba picked up full and not yet verted can't be marked empty
        just by dropping it at an almacén instead of an obra."""
        AssetTransaction.objects.create(
            asset=self.asset, company=self.company, from_zone=self.origin_zone,
            transaction_type=TransactionTypeChoices.MOVEMENT, performed_by=self.employee,
            planning=self.planning, data={"cubaEstado": "Cuba llena"},
        )
        almacen = make_zone(self.company, name="Almacen", lat=40.42, lon=-3.71)
        almacen.zone_type = WorkingZone.ZoneType.ALMACEN
        almacen.save()
        response = self._run_delivery(extra_data={"cubaEstado": "Cuba vacía", "zone_id": almacen.id})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(AssetTransaction.objects.filter(transaction_type=TransactionTypeChoices.DELIVERY).count(), 0)


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
        self.planning = make_planning(
            self.company, asset=self.asset, assigned_to=self.employee,
            status=PlanningStatus.IN_TRANSIT,
            cuba_operation_type=CubaOperationType.RETIRADA,
        )
        self.movement_tx = AssetTransaction.objects.create(
            asset=self.asset,
            company=self.company,
            transaction_type=TransactionTypeChoices.MOVEMENT,
            from_zone=self.zone,
            performed_by=self.employee,
            planning=self.planning,
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
            "planning_id": self.planning.id,
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

    def test_receipt_response_includes_the_related_movement_document(self):
        """The driver needs both documents after a verteo -- the verteo's own
        plus the cuba pickup it discharges -- for an inspector."""
        response = self._run_receipt()
        body = json.loads(response.content)
        self.assertEqual(body["related_transaction_id"], self.movement_tx.id)
        self.assertEqual(body["related_transaction_type"], TransactionTypeChoices.MOVEMENT)
        self.assertIn(self.movement_tx.public_token, body["related_document_url"])

    def test_receipt_response_omits_related_keys_when_no_movement_to_link(self):
        """No prior movement for this asset -- e.g. verted straight from a
        planning with no pickup on record -- must omit the keys entirely,
        not send them as null, so the app's "only show if present" logic works."""
        AssetTransaction.objects.filter(pk=self.movement_tx.pk).delete()
        response = self._run_receipt()
        body = json.loads(response.content)
        self.assertNotIn("related_transaction_id", body)
        self.assertNotIn("related_transaction_type", body)
        self.assertNotIn("related_document_url", body)

    # Test 25
    def test_receipt_reference_number_passes_through_to_data(self):
        self._run_receipt(extra_data={"reference_number": "REF-003"})
        tx = AssetTransaction.objects.filter(transaction_type=TransactionTypeChoices.RECEIPT).first()
        self.assertEqual(tx.data.get("reference_number"), "REF-003")

    def test_receipt_requires_nro_albaran(self):
        """
        nro_albaran is required for a verteo.
        Sending an empty string must return an error response.
        """
        response = self._run_receipt(extra_data={"nro_albaran": ""})
        body = json.loads(response.content)
        self.assertIn("error", body)
        self.assertEqual(response.status_code, 400)

    def test_receipt_without_planning_id_is_rejected(self):
        response = self._run_receipt(extra_data={"planning_id": None})
        self.assertEqual(response.status_code, 400)
        self.assertIn("error", json.loads(response.content))

    def test_receipt_inherits_project_file_from_the_pending_movement(self):
        from management.models import ProjectFile
        from sales.models import Client

        client = Client.objects.create(name="Client 1", company=self.company)
        project_file = ProjectFile.objects.create(name="Project 1", client=client, company=self.company)
        self.movement_tx.project_file = project_file
        self.movement_tx.save(update_fields=["project_file"])

        self._run_receipt()
        tx = AssetTransaction.objects.filter(transaction_type=TransactionTypeChoices.RECEIPT).first()
        self.assertEqual(tx.project_file_id, project_file.id)

    def test_receipt_links_to_movement_picked_up_from_an_almacen(self):
        """Regression: movement_tx used to be filtered to from_zone__zone_type=OBRA,
        losing the link when the pending pickup came from an almacén instead —
        contradicts Asset.verteo_pending_transaction's own docstring, which
        explicitly allows that case."""
        from company.models import WorkingZone

        almacen = make_zone(self.company, name="Almacen 2")
        almacen.zone_type = WorkingZone.ZoneType.ALMACEN
        almacen.save()
        from datetime import timedelta

        almacen_movement = AssetTransaction.objects.create(
            asset=self.asset, company=self.company, from_zone=almacen,
            transaction_type=TransactionTypeChoices.MOVEMENT, performed_by=self.employee,
            date=self.movement_tx.date + timedelta(minutes=5),
        )
        self._run_receipt()
        receipt = AssetTransactionReceipt.objects.first()
        self.assertEqual(receipt.movement_transaction, almacen_movement)

    def test_receipt_with_planning_id_links_fk_and_closes_plan(self):
        """Verteo completes a retirada whether the cuba is still on the truck
        or was already dropped at an almacén."""
        self._run_receipt()
        tx = AssetTransaction.objects.filter(transaction_type=TransactionTypeChoices.RECEIPT).first()
        self.assertEqual(tx.planning_id, self.planning.id)
        self.planning.refresh_from_db()
        self.assertEqual(self.planning.status, PlanningStatus.DELIVERED)

    def test_receipt_against_a_puesta_planning_never_closes_it(self):
        """Same reasoning as MovementStrategyTests' equivalent — a puesta only
        closes via an actual delivery, never via a verteo registered against it."""
        self.planning.cuba_operation_type = CubaOperationType.PUESTA
        self.planning.save(update_fields=["cuba_operation_type"])
        original_status = self.planning.status
        self._run_receipt()
        self.planning.refresh_from_db()
        self.assertEqual(self.planning.status, original_status)

    def test_receipt_does_not_create_a_movement_when_verted_straight_off_the_truck(self):
        """No DELIVERY happened before this verteo — the cuba never left the
        truck, so there's no implicit re-pickup to register."""
        self._run_receipt()
        self.assertEqual(AssetTransaction.objects.filter(transaction_type=TransactionTypeChoices.MOVEMENT).count(), 1)

    def test_receipt_registers_an_implicit_pickup_when_verted_after_a_delivery(self):
        """Regression: a full cuba dropped off at an almacén, then verted from
        there, must show as back on the truck — the verteo implies picking it
        back up to take it to the vertedero. Otherwise Escanear cuba wrongly
        stays available and the Home "cuba sin resolver" card disappears even
        though the driver is still physically holding it."""
        almacen = make_zone(self.company, name="Almacen")
        almacen.zone_type = WorkingZone.ZoneType.ALMACEN
        almacen.save()
        AssetTransaction.objects.create(
            asset=self.asset, company=self.company, from_zone=self.zone, to_zone=almacen,
            transaction_type=TransactionTypeChoices.DELIVERY, performed_by=self.employee,
            planning=self.planning, data={"cubaEstado": "Cuba llena"},
        )
        self.asset.current_working_zone = almacen
        self.asset.save(update_fields=["current_working_zone"])

        self._run_receipt()

        movements = AssetTransaction.objects.filter(transaction_type=TransactionTypeChoices.MOVEMENT).order_by("date")
        self.assertEqual(movements.count(), 2)
        implicit_movement = movements.last()
        self.assertEqual(implicit_movement.data.get("cubaEstado"), "Cuba vacía")
        self.assertEqual(implicit_movement.from_zone_id, almacen.id)

        self.asset.refresh_from_db()
        self.assertEqual(self.asset.current_transaction_status(), AssetCurrentStatus.PICKED_UP)
        self.assertIsNone(self.asset.verteo_pending_transaction)
        self.assertTrue(Asset.objects.with_pending_pickup_by(self.employee).filter(pk=self.asset.pk).exists())


class CubaOperationCompletionTests(TestCase):
    """Puesta / retirada / cambio close only when that operation's work is done."""

    def setUp(self):
        self.company = make_company()
        self.user = make_user()
        self.employee = make_employee(self.company, self.user)
        self.asset_type = make_asset_type(self.company)
        self.obra = make_zone(self.company, name="Obra Destino", lat=40.42, lon=-3.71)
        self.almacen = make_zone(self.company, name="Almacen", lat=40.41, lon=-3.70)
        self.almacen.zone_type = WorkingZone.ZoneType.ALMACEN
        self.almacen.save(update_fields=["zone_type"])
        self.new_cuba = make_asset(
            self.company, self.asset_type, employee=self.employee, zone=self.almacen, name="CUBA-NEW",
        )
        self.old_cuba = make_asset(
            self.company, self.asset_type, employee=self.employee, zone=self.obra, name="CUBA-OLD",
        )

    def _plan(self, operation, asset=None, **overrides):
        return make_planning(
            self.company,
            asset=asset or self.new_cuba,
            assigned_to=self.employee,
            cuba_operation_type=operation,
            destination=self.obra,
            **overrides,
        )

    def _move(self, planning, asset, zone, extra_data=None):
        from api.fleet.views.assets import MovementStrategy

        asset.current_working_zone = zone
        asset.save(update_fields=["current_working_zone"])
        data = {
            "transaction_type": "movement",
            "latitude": 40.42,
            "longitude": -3.71,
            "zone_id": zone.id,
            "planning_id": planning.id,
            CubaTransactionKey.ESTADO: CubaFillState.EMPTY,
        }
        if extra_data:
            data.update(extra_data)
        return MovementStrategy(data, asset, self.employee, self.user).execute()

    def _deliver(self, planning, asset, zone, extra_data=None):
        from api.fleet.views.assets import DeliveryStrategy

        data = {
            "transaction_type": "delivery",
            "latitude": 40.42,
            "longitude": -3.71,
            "zone_id": zone.id,
            "planning_id": planning.id,
            CubaTransactionKey.ESTADO: CubaFillState.EMPTY,
        }
        if extra_data:
            data.update(extra_data)
        return DeliveryStrategy(data, asset, self.employee, self.user).execute()

    def _vert(self, planning, asset):
        from api.fleet.views.assets import ReceiptStrategy

        return ReceiptStrategy(
            {
                "transaction_type": "receipt",
                "nro_albaran": "ALB-001",
                "foto_albaran": "",
                "peso_neto": "500",
                "material_vertido": "RCD Limpio",
                "zone_id": self.obra.id,
                "planning_id": planning.id,
            },
            asset, self.employee, self.user,
        ).execute()

    def test_cambio_empty_warehouse_pickup_does_not_close(self):
        planning = self._plan(CubaOperationType.CAMBIO)
        self._move(planning, self.new_cuba, self.almacen)
        planning.refresh_from_db()
        self.assertEqual(planning.status, PlanningStatus.IN_TRANSIT)

    def test_cambio_warehouse_pickup_then_obra_dropoff_does_not_close(self):
        planning = self._plan(CubaOperationType.CAMBIO)
        self._move(planning, self.new_cuba, self.almacen)
        self._deliver(planning, self.new_cuba, self.obra)
        planning.refresh_from_db()
        self.assertEqual(planning.status, PlanningStatus.IN_TRANSIT)

    def test_cambio_closes_after_obra_dropoff_and_empty_obra_pickup(self):
        planning = self._plan(CubaOperationType.CAMBIO)
        self._move(planning, self.new_cuba, self.almacen)
        self._deliver(planning, self.new_cuba, self.obra)
        planning.asset = self.old_cuba
        planning.save(update_fields=["asset"])
        self._move(planning, self.old_cuba, self.obra)
        planning.refresh_from_db()
        self.assertEqual(planning.status, PlanningStatus.DELIVERED)

    def test_cambio_closes_when_obra_pickup_happens_before_dropoff(self):
        planning = self._plan(CubaOperationType.CAMBIO, asset=self.old_cuba)
        self._move(planning, self.old_cuba, self.obra)
        planning.refresh_from_db()
        self.assertEqual(planning.status, PlanningStatus.IN_TRANSIT)
        planning.asset = self.new_cuba
        planning.save(update_fields=["asset"])
        self._deliver(planning, self.new_cuba, self.obra)
        planning.refresh_from_db()
        self.assertEqual(planning.status, PlanningStatus.DELIVERED)

    def test_cambio_full_obra_pickup_needs_verteo_and_dropoff(self):
        planning = self._plan(CubaOperationType.CAMBIO, asset=self.old_cuba)
        self._move(planning, self.old_cuba, self.obra, extra_data={
            CubaTransactionKey.ESTADO: CubaFillState.FULL,
        })
        planning.refresh_from_db()
        self.assertEqual(planning.status, PlanningStatus.IN_TRANSIT)
        self._vert(planning, self.old_cuba)
        planning.refresh_from_db()
        self.assertEqual(planning.status, PlanningStatus.IN_TRANSIT)
        planning.asset = self.new_cuba
        planning.save(update_fields=["asset"])
        self._deliver(planning, self.new_cuba, self.obra)
        planning.refresh_from_db()
        self.assertEqual(planning.status, PlanningStatus.DELIVERED)

    def test_cambio_agua_follows_the_same_two_leg_rule(self):
        planning = self._plan(CubaOperationType.CAMBIO_AGUA)
        self._move(planning, self.new_cuba, self.almacen)
        self._deliver(planning, self.new_cuba, self.obra)
        planning.refresh_from_db()
        self.assertEqual(planning.status, PlanningStatus.IN_TRANSIT)

    def test_puesta_only_closes_on_destination_dropoff(self):
        planning = self._plan(CubaOperationType.PUESTA)
        self._move(planning, self.new_cuba, self.almacen)
        planning.refresh_from_db()
        self.assertEqual(planning.status, PlanningStatus.IN_TRANSIT)
        self._deliver(planning, self.new_cuba, self.almacen)
        planning.refresh_from_db()
        self.assertEqual(planning.status, PlanningStatus.IN_TRANSIT)
        self._deliver(planning, self.new_cuba, self.obra)
        planning.refresh_from_db()
        self.assertEqual(planning.status, PlanningStatus.DELIVERED)

    def test_retirada_does_not_close_on_delivery_of_a_full_cuba(self):
        planning = self._plan(CubaOperationType.RETIRADA, asset=self.old_cuba)
        self._move(planning, self.old_cuba, self.obra, extra_data={
            CubaTransactionKey.ESTADO: CubaFillState.FULL,
        })
        planning.refresh_from_db()
        self.assertEqual(planning.status, PlanningStatus.IN_TRANSIT)
        self._deliver(planning, self.old_cuba, self.almacen, extra_data={
            CubaTransactionKey.ESTADO: CubaFillState.FULL,
        })
        planning.refresh_from_db()
        self.assertEqual(planning.status, PlanningStatus.IN_TRANSIT)
        self._vert(planning, self.old_cuba)
        planning.refresh_from_db()
        self.assertEqual(planning.status, PlanningStatus.DELIVERED)

    def test_cambio_in_progress_stays_the_next_task_without_a_stuck_cuba_card(self):
        planning = self._plan(CubaOperationType.CAMBIO)
        self._move(planning, self.new_cuba, self.almacen)
        client = APIClient()
        client.force_authenticate(user=self.user)
        body = client.get("/en/api/plannings/next/").json()
        self.assertEqual(body["task"]["id"], planning.id)
        self.assertIsNone(body["pending_dropoff"])

    def test_retirada_empty_pickup_surfaces_pending_dropoff_for_the_next_job(self):
        planning = self._plan(CubaOperationType.RETIRADA, asset=self.old_cuba)
        self._move(planning, self.old_cuba, self.obra)
        next_plan = make_planning(
            self.company, asset=self.new_cuba, assigned_to=self.employee,
            cuba_operation_type=CubaOperationType.PUESTA, destination=self.obra,
        )
        client = APIClient()
        client.force_authenticate(user=self.user)
        body = client.get("/en/api/plannings/next/").json()
        self.assertEqual(body["task"]["id"], next_plan.id)
        self.assertIsNotNone(body["pending_dropoff"])
        self.assertEqual(body["pending_dropoff"]["asset_id"], self.old_cuba.id)
        self.assertFalse(body["pending_dropoff"]["verteo_pending"])

    def test_cambio_misma_cuba_closes_after_pickup_and_dropoff_of_the_same_asset(self):
        planning = self._plan(CubaOperationType.CAMBIO_MISMA_CUBA, asset=self.old_cuba)
        self._move(planning, self.old_cuba, self.obra)
        planning.refresh_from_db()
        self.assertEqual(planning.status, PlanningStatus.IN_TRANSIT)
        self._deliver(planning, self.old_cuba, self.obra)
        planning.refresh_from_db()
        self.assertEqual(planning.status, PlanningStatus.DELIVERED)
        self.old_cuba.refresh_from_db()
        self.assertEqual(self.old_cuba.current_transaction_status(), "DELIVERED")

    def test_cambio_misma_cuba_full_pickup_needs_verteo_before_close(self):
        planning = self._plan(CubaOperationType.CAMBIO_MISMA_CUBA, asset=self.old_cuba)
        self._move(planning, self.old_cuba, self.obra, extra_data={
            CubaTransactionKey.ESTADO: CubaFillState.FULL,
        })
        planning.refresh_from_db()
        self.assertEqual(planning.status, PlanningStatus.IN_TRANSIT)
        self._vert(planning, self.old_cuba)
        planning.refresh_from_db()
        self.assertEqual(planning.status, PlanningStatus.IN_TRANSIT)
        self._deliver(planning, self.old_cuba, self.obra)
        planning.refresh_from_db()
        self.assertEqual(planning.status, PlanningStatus.DELIVERED)
        self.old_cuba.refresh_from_db()
        self.assertEqual(self.old_cuba.current_transaction_status(), "DELIVERED")

    def test_cambio_misma_cuba_next_task_hides_the_use_pending_cuba_shortcut(self):
        """The truck must arrive empty for this operation type — the driver can't
        skip the pickup by reusing a cuba they're still carrying from a previous job."""
        planning = self._plan(CubaOperationType.RETIRADA, asset=self.old_cuba)
        self._move(planning, self.old_cuba, self.obra)
        make_planning(
            self.company, asset=None, assigned_to=self.employee,
            cuba_operation_type=CubaOperationType.CAMBIO_MISMA_CUBA, destination=self.almacen,
        )
        client = APIClient()
        client.force_authenticate(user=self.user)
        body = client.get("/en/api/plannings/next/").json()
        self.assertIsNotNone(body["pending_dropoff"])
        self.assertFalse(body["pending_dropoff"]["can_use_for_next_task"])

    def test_cambio_agua_closes_after_pickup_and_dropoff_of_the_same_asset(self):
        """cambio_agua is a same-asset round trip like cambio_misma_cuba, not a
        real two-cuba swap — no verteo step applies."""
        planning = self._plan(CubaOperationType.CAMBIO_AGUA, asset=self.old_cuba)
        self._move(planning, self.old_cuba, self.obra)
        planning.refresh_from_db()
        self.assertEqual(planning.status, PlanningStatus.IN_TRANSIT)
        self._deliver(planning, self.old_cuba, self.obra)
        planning.refresh_from_db()
        self.assertEqual(planning.status, PlanningStatus.DELIVERED)

    def test_cambio_closes_when_the_vieja_pickup_is_the_same_asset_just_delivered(self):
        """Real scenario, not an edge case: dejar la cuba nueva (vacía), esperar
        un rato, recoger esa misma cuba de vuelta (ahora llena) para verterla y
        quedársela en el camión — sin una segunda cuba física distinta."""
        planning = self._plan(CubaOperationType.CAMBIO)
        self._deliver(planning, self.new_cuba, self.obra)
        planning.refresh_from_db()
        self.assertEqual(planning.status, PlanningStatus.IN_TRANSIT)
        self._move(planning, self.new_cuba, self.obra, extra_data={
            CubaTransactionKey.ESTADO: CubaFillState.FULL,
        })
        planning.refresh_from_db()
        self.assertEqual(planning.status, PlanningStatus.IN_TRANSIT)
        self._vert(planning, self.new_cuba)
        planning.refresh_from_db()
        self.assertEqual(planning.status, PlanningStatus.DELIVERED)

    def test_cambio_closes_immediately_when_the_same_asset_comes_back_empty(self):
        planning = self._plan(CubaOperationType.CAMBIO)
        self._deliver(planning, self.new_cuba, self.obra)
        self._move(planning, self.new_cuba, self.obra)
        planning.refresh_from_db()
        self.assertEqual(planning.status, PlanningStatus.DELIVERED)

    def test_cambio_an_earlier_unrelated_verteo_does_not_resolve_a_later_full_pickup(self):
        """Regression (real data, plan 225): a prep pickup+verteo at the almacén
        before dejar (leg 1 doesn't even count toward the two-leg check, since
        it's not from the destination zone) must not be reused to resolve a
        LATER, separate full pickup at the obra (leg 2) — that one needs its
        own verteo."""
        planning = self._plan(CubaOperationType.CAMBIO)
        self._move(planning, self.new_cuba, self.almacen, extra_data={
            CubaTransactionKey.ESTADO: CubaFillState.FULL,
        })
        self._vert(planning, self.new_cuba)
        self._deliver(planning, self.new_cuba, self.obra)
        planning.asset = self.old_cuba
        planning.save(update_fields=["asset"])
        self._move(planning, self.old_cuba, self.obra, extra_data={
            CubaTransactionKey.ESTADO: CubaFillState.FULL,
        })
        planning.refresh_from_db()
        self.assertEqual(planning.status, PlanningStatus.IN_TRANSIT)
        self._vert(planning, self.old_cuba)
        planning.refresh_from_db()
        self.assertEqual(planning.status, PlanningStatus.DELIVERED)

    def test_cambio_dejar_alone_with_no_prior_pickup_does_not_close(self):
        """Truck already carrying the new cuba (pre-assigned / carried over — no
        MovementStrategy call ever happened for this planning) — a single dejar
        must not close a CAMBIO plan by itself; leg 2 (recoger vieja) still owed."""
        planning = self._plan(CubaOperationType.CAMBIO)
        self._deliver(planning, self.new_cuba, self.obra)
        planning.refresh_from_db()
        self.assertEqual(planning.status, PlanningStatus.IN_TRANSIT)

    def test_cambio_agua_next_task_hides_the_use_pending_cuba_shortcut(self):
        planning = self._plan(CubaOperationType.RETIRADA, asset=self.old_cuba)
        self._move(planning, self.old_cuba, self.obra)
        make_planning(
            self.company, asset=None, assigned_to=self.employee,
            cuba_operation_type=CubaOperationType.CAMBIO_AGUA, destination=self.almacen,
        )
        client = APIClient()
        client.force_authenticate(user=self.user)
        body = client.get("/en/api/plannings/next/").json()
        self.assertIsNotNone(body["pending_dropoff"])
        self.assertFalse(body["pending_dropoff"]["can_use_for_next_task"])

    def test_retirada_next_task_hides_the_use_pending_cuba_shortcut(self):
        """A retirada can't be satisfied with a cuba already on the truck —
        the driver must drop it off first."""
        planning = self._plan(CubaOperationType.RETIRADA, asset=self.old_cuba)
        self._move(planning, self.old_cuba, self.obra)
        make_planning(
            self.company, asset=None, assigned_to=self.employee,
            cuba_operation_type=CubaOperationType.RETIRADA, destination=self.almacen,
        )
        client = APIClient()
        client.force_authenticate(user=self.user)
        body = client.get("/en/api/plannings/next/").json()
        self.assertIsNotNone(body["pending_dropoff"])
        self.assertFalse(body["pending_dropoff"]["can_use_for_next_task"])


class CubaChecklistTests(TestCase):
    """AssetPlanning.cuba_checklist() — the progress steps behind the San Carlos
    app's checkpoint stepper."""

    def setUp(self):
        self.company = make_company()
        self.user = make_user()
        self.employee = make_employee(self.company, self.user)
        self.asset_type = make_asset_type(self.company)
        self.obra = make_zone(self.company, name="Obra Destino", lat=40.42, lon=-3.71)
        self.almacen = make_zone(self.company, name="Almacen", lat=40.41, lon=-3.70)
        self.almacen.zone_type = WorkingZone.ZoneType.ALMACEN
        self.almacen.save(update_fields=["zone_type"])
        self.cuba = make_asset(self.company, self.asset_type, employee=self.employee, zone=self.obra, name="CUBA-1")

    def _plan(self, operation, **overrides):
        return make_planning(
            self.company, asset=self.cuba, assigned_to=self.employee,
            cuba_operation_type=operation, destination=self.obra, **overrides,
        )

    def _move(self, planning, zone, extra_data=None):
        from api.fleet.views.assets import MovementStrategy

        self.cuba.current_working_zone = zone
        self.cuba.save(update_fields=["current_working_zone"])
        data = {
            "transaction_type": "movement", "latitude": 40.42, "longitude": -3.71,
            "zone_id": zone.id, "planning_id": planning.id,
            CubaTransactionKey.ESTADO: CubaFillState.EMPTY,
        }
        if extra_data:
            data.update(extra_data)
        MovementStrategy(data, self.cuba, self.employee, self.user).execute()

    def _deliver(self, planning, zone):
        from api.fleet.views.assets import DeliveryStrategy

        data = {
            "transaction_type": "delivery", "latitude": 40.42, "longitude": -3.71,
            "zone_id": zone.id, "planning_id": planning.id,
            CubaTransactionKey.ESTADO: CubaFillState.EMPTY,
        }
        DeliveryStrategy(data, self.cuba, self.employee, self.user).execute()

    def _vert(self, planning):
        from api.fleet.views.assets import ReceiptStrategy

        ReceiptStrategy(
            {
                "transaction_type": "receipt", "nro_albaran": "ALB-1", "foto_albaran": "",
                "peso_neto": "500", "material_vertido": "RCD Limpio",
                "zone_id": self.obra.id, "planning_id": planning.id,
            },
            self.cuba, self.employee, self.user,
        ).execute()

    def _statuses(self, planning):
        planning.refresh_from_db()
        return {s["key"]: s["status"] for s in planning.cuba_checklist()}

    def test_puesta_checklist_starts_with_uncollected_cuba(self):
        planning = self._plan(CubaOperationType.PUESTA)
        self.assertEqual(
            self._statuses(planning),
            {"recoger_cuba": "pending", "dejar": "pending"},
        )
        self._deliver(planning, self.obra)
        self.assertEqual(
            self._statuses(planning),
            {"recoger_cuba": "pending", "dejar": "done"},
        )

    def test_puesta_checklist_does_not_mark_recoger_done_from_dispatch_asset(self):
        planning = self._plan(CubaOperationType.PUESTA)
        self.assertTrue(planning.asset_id)
        self.assertEqual(self._statuses(planning)["recoger_cuba"], "pending")

    def test_retirada_checklist_skips_verter_when_empty(self):
        planning = self._plan(CubaOperationType.RETIRADA)
        self.assertEqual(
            self._statuses(planning),
            {"recoger_cuba": "pending", "recoger": "pending", "verter": "pending"},
        )
        self._move(planning, self.obra)
        self.assertEqual(
            self._statuses(planning),
            {"recoger_cuba": "done", "recoger": "done", "verter": "skipped"},
        )

    def test_retirada_checklist_needs_verter_when_full(self):
        planning = self._plan(CubaOperationType.RETIRADA)
        self._move(planning, self.obra, extra_data={CubaTransactionKey.ESTADO: CubaFillState.FULL})
        self.assertEqual(
            self._statuses(planning),
            {"recoger_cuba": "done", "recoger": "done", "verter": "pending"},
        )
        self._vert(planning)
        self.assertEqual(
            self._statuses(planning),
            {"recoger_cuba": "done", "recoger": "done", "verter": "done"},
        )

    def test_cambio_misma_cuba_checklist_full_cycle(self):
        planning = self._plan(CubaOperationType.CAMBIO_MISMA_CUBA)
        self.assertEqual(
            [s["key"] for s in planning.cuba_checklist()],
            ["recoger_cuba", "recoger", "verter", "dejar"],
        )
        self.assertEqual(
            self._statuses(planning),
            {"recoger_cuba": "pending", "recoger": "pending", "verter": "pending", "dejar": "pending"},
        )
        self._move(planning, self.obra, extra_data={CubaTransactionKey.ESTADO: CubaFillState.FULL})
        self.assertEqual(
            self._statuses(planning),
            {"recoger_cuba": "done", "recoger": "done", "verter": "pending", "dejar": "pending"},
        )
        self._vert(planning)
        self.assertEqual(
            self._statuses(planning),
            {"recoger_cuba": "done", "recoger": "done", "verter": "done", "dejar": "pending"},
        )
        self._deliver(planning, self.obra)
        self.assertEqual(
            self._statuses(planning),
            {"recoger_cuba": "done", "recoger": "done", "verter": "done", "dejar": "done"},
        )

    def test_cambio_agua_checklist_has_no_verter_step(self):
        planning = self._plan(CubaOperationType.CAMBIO_AGUA)
        self.assertEqual(
            self._statuses(planning),
            {"recoger_cuba": "pending", "recoger": "pending", "dejar": "pending"},
        )
        self._move(planning, self.obra)
        self._deliver(planning, self.obra)
        self.assertEqual(
            self._statuses(planning),
            {"recoger_cuba": "done", "recoger": "done", "dejar": "done"},
        )

    def test_cambio_checklist_tracks_both_legs_independently(self):
        old_cuba = make_asset(self.company, self.asset_type, employee=self.employee, zone=self.obra, name="CUBA-OLD")
        planning = self._plan(CubaOperationType.CAMBIO)
        self.assertEqual(
            self._statuses(planning),
            {
                "recoger_cuba": "pending",
                "dejar_nueva": "pending",
                "recoger_vieja": "pending",
                "verter": "pending",
            },
        )
        self._deliver(planning, self.obra)
        self.assertEqual(
            self._statuses(planning),
            {
                "recoger_cuba": "pending",
                "dejar_nueva": "done",
                "recoger_vieja": "pending",
                "verter": "pending",
            },
        )
        planning.asset = old_cuba
        planning.save(update_fields=["asset"])
        self.cuba = old_cuba
        self._move(planning, self.obra)
        self.assertEqual(
            self._statuses(planning),
            {
                "recoger_cuba": "done",
                "dejar_nueva": "done",
                "recoger_vieja": "done",
                "verter": "skipped",
            },
        )

    def test_status_color_flags_in_transit_with_a_pending_verteo(self):
        from assets.models import VERTEO_PENDING_COLOR, STATUS_COLORS

        planning = self._plan(CubaOperationType.RETIRADA)
        self.assertEqual(planning.status_color, STATUS_COLORS[PlanningStatus.PENDING])
        self._move(planning, self.obra, extra_data={CubaTransactionKey.ESTADO: CubaFillState.FULL})
        planning.refresh_from_db()
        self.assertEqual(planning.status, PlanningStatus.IN_TRANSIT)
        self.assertTrue(planning.has_pending_verteo())
        self.assertEqual(planning.status_color, VERTEO_PENDING_COLOR)
        self._vert(planning)
        planning.refresh_from_db()
        self.assertEqual(planning.status, PlanningStatus.DELIVERED)
        self.assertFalse(planning.has_pending_verteo())
        self.assertEqual(planning.status_color, STATUS_COLORS[PlanningStatus.DELIVERED])

    def test_status_color_stays_plain_in_transit_for_an_empty_pickup_still_awaiting_dropoff(self):
        from assets.models import STATUS_COLORS

        planning = self._plan(CubaOperationType.CAMBIO_MISMA_CUBA)
        self._move(planning, self.obra)
        planning.refresh_from_db()
        self.assertEqual(planning.status, PlanningStatus.IN_TRANSIT)
        self.assertFalse(planning.has_pending_verteo())
        self.assertEqual(planning.status_color, STATUS_COLORS[PlanningStatus.IN_TRANSIT])

