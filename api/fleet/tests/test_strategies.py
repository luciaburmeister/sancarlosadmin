"""
api/fleet/tests/test_strategies.py

MovementStrategy, DeliveryStrategy, ReceiptStrategy

Run just this file:
    python manage.py test api.fleet.tests.test_strategies
"""

import json
from unittest.mock import patch

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
    DriverFixtureMixin,
    make_almacen,
    make_asset,
    make_asset_type,
    make_employee,
    make_planning,
    make_user,
    make_zone,
    run_strategy,
)
from company.models import WorkingZone


class MovementStrategyTests(DriverFixtureMixin, TestCase):
    """
    Tests for MovementStrategy — the code that runs when a driver
    does a recogida (picks up a cuba).
    """

    def setUp(self):
        super().setUp()
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
        data = {
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
        return run_strategy("movement", self.asset, self.employee, self.user, data)

    def test_movement_saves_one_transaction_with_albaran_fields_and_responds_with_its_id(self):
        """
        One execute() -> exactly one MOVEMENT row carrying the albaran fields
        the app sent (in the data JSON field), and a response with
        'success': True plus the 'transaction_id' the Android app reads.
        """
        response = self._run_movement()
        body = json.loads(response.content)
        self.assertTrue(body["success"])
        self.assertIn("transaction_id", body)
        self.assertEqual(AssetTransaction.objects.count(), 1)
        tx = AssetTransaction.objects.first()
        self.assertEqual(tx.transaction_type, TransactionTypeChoices.MOVEMENT)
        self.assertEqual(tx.data.get("matricula"), "1234ABC")
        self.assertEqual(tx.data.get("tipo_camion"), "Volquete")
        self.assertEqual(tx.data.get("implemento"), "Gancho")
        self.assertEqual(tx.data.get("cliente"), "")  # filled from zone.client; test zone has no client
        self.assertEqual(tx.data.get("obra"), "Obra Test")
        self.assertEqual(tx.data.get("trabajo"), "Recogida")
        self.assertEqual(tx.data.get("tipo_cuba"), "6m3")
        self.assertEqual(tx.data.get("m3"), "6")
        self.assertEqual(tx.data.get("vertedero"), "Vertedero Norte")

    def test_movement_uses_the_planning_project_file_over_the_zone(self):
        from management.models import ProjectFile
        from sales.models import Client

        client = Client.objects.create(name="Client 1", company=self.company)
        zone_file = ProjectFile.objects.create(name="Zone", client=client, company=self.company)
        plan_file = ProjectFile.objects.create(name="Plan", client=client, company=self.company)
        self.zone.project_file = zone_file
        self.zone.save(update_fields=["project_file"])
        self.planning.project_file = plan_file
        self.planning.save(update_fields=["project_file"])

        response = self._run_movement()
        self.assertEqual(response.status_code, 200)
        tx = AssetTransaction.objects.get()
        self.assertEqual(tx.project_file_id, plan_file.pk)

    @patch("core.tasks.send_asset_transaction_email_task.delay")
    def test_movement_emails_the_named_landfill_when_automatic_send_is_on(self, delay):
        landfill = make_zone(self.company, name="Vertedero Norte", zone_type=WorkingZone.ZoneType.VERTEDERO)
        landfill.receipt_notification_email = "vertedero@example.com"
        landfill.auto_send_receipt = True
        landfill.save()

        with self.captureOnCommitCallbacks(execute=True):
            self._run_movement()

        delay.assert_called_once()
        self.assertEqual(delay.call_args.kwargs["to_email"], "vertedero@example.com")
        pickup = AssetTransaction.objects.get(transaction_type=TransactionTypeChoices.MOVEMENT)
        self.assertIsNone(pickup.to_zone_id)

    @patch("core.tasks.send_asset_transaction_email_task.delay")
    def test_movement_does_not_email_when_automatic_send_is_off(self, delay):
        landfill = make_zone(self.company, name="Vertedero Norte", zone_type=WorkingZone.ZoneType.VERTEDERO)
        landfill.receipt_notification_email = "vertedero@example.com"
        landfill.save()

        self._run_movement()

        delay.assert_not_called()

    # reference_number is a real AssetTransaction field, not a `data` key --
    # pulled out of the incoming payload explicitly before the rest falls
    # through into form_data, so it never gets duplicated into the JSON blob.
    def test_movement_reference_number_is_stored_on_the_model_field(self):
        self._run_movement(extra_data={"reference_number": "REF-001"})
        tx = AssetTransaction.objects.first()
        self.assertEqual(tx.reference_number, "REF-001")
        self.assertNotIn("reference_number", tx.data)

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


class DeliveryStrategyTests(DriverFixtureMixin, TestCase):
    """Tests for DeliveryStrategy — when a driver delivers (deja) a cuba."""

    def setUp(self):
        super().setUp()
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
        data = {
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
        return run_strategy("delivery", self.asset, self.employee, self.user, data)

    def test_delivery_saves_one_transaction_with_albaran_fields_and_moves_the_asset(self):
        """
        One delivery -> exactly one DELIVERY row carrying the albaran fields in
        its data JSON field, and the asset's current_working_zone moved to the
        destination zone.
        """
        self._run_delivery()
        self.assertEqual(AssetTransaction.objects.count(), 1)
        tx = AssetTransaction.objects.first()
        self.assertEqual(tx.transaction_type, TransactionTypeChoices.DELIVERY)
        self.assertEqual(tx.data.get("matricula"), "1234ABC")
        self.assertEqual(tx.data.get("tipo_camion"), "Volquete")
        self.assertEqual(tx.data.get("tipo_cuba"), "6m3")
        self.asset.refresh_from_db()
        self.assertEqual(self.asset.current_working_zone, self.dest_zone)

    def test_delivery_reference_number_is_stored_on_the_model_field(self):
        self._run_delivery(extra_data={"reference_number": "REF-002"})
        tx = AssetTransaction.objects.first()
        self.assertEqual(tx.reference_number, "REF-002")
        self.assertNotIn("reference_number", tx.data)

    def test_delivery_without_planning_id_is_rejected(self):
        response = self._run_delivery(extra_data={"planning_id": None})
        self.assertEqual(response.status_code, 400)
        self.assertIn("error", json.loads(response.content))
        self.assertEqual(AssetTransaction.objects.count(), 0)

    def test_delivery_after_porte_with_no_next_cuba_plan(self):
        """Finished porte → pending_dropoff still points at that porte id.
        Leaving the cuba must work with no next CUBA plan (don't 400)."""
        almacen = make_almacen(self.company, lat=40.42, lon=-3.71)
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

    def test_delivery_uses_the_planning_project_file_over_the_destination_zone(self):
        from management.models import ProjectFile
        from sales.models import Client

        client = Client.objects.create(name="Client 1", company=self.company)
        zone_file = ProjectFile.objects.create(name="Zone", client=client, company=self.company)
        plan_file = ProjectFile.objects.create(name="Plan", client=client, company=self.company)
        self.dest_zone.project_file = zone_file
        self.dest_zone.save(update_fields=["project_file"])
        self.planning.project_file = plan_file
        self.planning.save(update_fields=["project_file"])

        response = self._run_delivery()
        self.assertEqual(response.status_code, 200)
        tx = AssetTransaction.objects.get(transaction_type=TransactionTypeChoices.DELIVERY)
        self.assertEqual(tx.project_file_id, plan_file.pk)

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
        almacen = make_almacen(self.company, lat=40.42, lon=-3.71)
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
        almacen = make_almacen(self.company, lat=40.42, lon=-3.71)
        response = self._run_delivery(extra_data={"cubaEstado": "Cuba vacía", "zone_id": almacen.id})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(AssetTransaction.objects.filter(transaction_type=TransactionTypeChoices.DELIVERY).count(), 0)


class ReceiptStrategyTests(DriverFixtureMixin, TestCase):
    """Tests for ReceiptStrategy — when a driver registers a verteo."""

    def setUp(self):
        super().setUp()
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
        data = {
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
        return run_strategy("receipt", self.asset, self.employee, self.user, data)

    def test_receipt_saves_transaction_and_receipt_row_linked_to_the_original_movement(self):
        """
        After a verteo: one RECEIPT transaction plus exactly one
        AssetTransactionReceipt row pointing at it, with nro_albaran on the
        row itself, peso_neto / material_vertido in its data JSON field, and
        the movement_transaction FK set to the original pickup.
        """
        self._run_receipt()
        self.assertTrue(
            AssetTransaction.objects.filter(
                transaction_type=TransactionTypeChoices.RECEIPT
            ).exists()
        )
        self.assertEqual(AssetTransactionReceipt.objects.count(), 1)
        tx = AssetTransaction.objects.filter(
            transaction_type=TransactionTypeChoices.RECEIPT
        ).first()
        receipt = AssetTransactionReceipt.objects.first()
        self.assertEqual(receipt.transaction, tx)
        self.assertEqual(receipt.nro_albaran, "ALB-001")
        self.assertEqual(receipt.data.get("peso_neto"), "500")
        self.assertEqual(receipt.data.get("material_vertido"), "RCD Limpio")
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

    def test_receipt_reference_number_is_stored_on_the_model_field(self):
        self._run_receipt(extra_data={"reference_number": "REF-003"})
        tx = AssetTransaction.objects.filter(transaction_type=TransactionTypeChoices.RECEIPT).first()
        self.assertEqual(tx.reference_number, "REF-003")
        self.assertNotIn("reference_number", tx.data)

    def test_receipt_save_failure_rolls_back_the_transaction_too(self):
        """Regression: the RECEIPT AssetTransaction and its AssetTransactionReceipt
        used to be two separate, non-atomic writes -- a failure on the receipt
        save (e.g. DI numbering) left an orphaned transaction invisible to the
        Receipt admin's search."""
        with patch("assets.models.AssetTransactionReceipt.save", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                self._run_receipt()
        self.assertFalse(
            AssetTransaction.objects.filter(transaction_type=TransactionTypeChoices.RECEIPT).exists()
        )
        self.assertEqual(AssetTransactionReceipt.objects.count(), 0)

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

    def test_receipt_prefers_the_planning_project_file_over_the_movement(self):
        from management.models import ProjectFile
        from sales.models import Client

        client = Client.objects.create(name="Client 1", company=self.company)
        movement_file = ProjectFile.objects.create(name="Movement", client=client, company=self.company)
        plan_file = ProjectFile.objects.create(name="Plan", client=client, company=self.company)
        self.movement_tx.project_file = movement_file
        self.movement_tx.save(update_fields=["project_file"])
        self.planning.project_file = plan_file
        self.planning.save(update_fields=["project_file"])

        self._run_receipt()
        tx = AssetTransaction.objects.get(transaction_type=TransactionTypeChoices.RECEIPT)
        self.assertEqual(tx.project_file_id, plan_file.pk)

    def test_receipt_links_to_movement_picked_up_from_an_almacen(self):
        """Regression: movement_tx used to be filtered to from_zone__zone_type=OBRA,
        losing the link when the pending pickup came from an almacén instead —
        contradicts Asset.verteo_pending_transaction's own docstring, which
        explicitly allows that case."""
        almacen = make_almacen(self.company, name="Almacen 2")
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
        almacen = make_almacen(self.company)
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


class CubaFlowTestCase(DriverFixtureMixin, TestCase):
    """Shared obra/almacén fixture + cuba steps run straight through the
    strategies, all from the obra's GPS point. Each step defaults to an
    empty cuba (cubaEstado)."""

    def setUp(self):
        super().setUp()
        self.asset_type = make_asset_type(self.company)
        self.obra = make_zone(self.company, name="Obra Destino", lat=40.42, lon=-3.71)
        self.almacen = make_almacen(self.company, lat=40.41, lon=-3.70)

    def _payload(self, planning, zone, extra_data=None):
        data = {
            "latitude": 40.42,
            "longitude": -3.71,
            "zone_id": zone.id,
            "planning_id": planning.id,
            CubaTransactionKey.ESTADO: CubaFillState.EMPTY,
        }
        if extra_data:
            data.update(extra_data)
        return data

    def _move(self, planning, asset, zone, extra_data=None):
        """Recogida at `zone` (the asset is parked there first)."""
        return run_strategy(
            "movement", asset, self.employee, self.user,
            self._payload(planning, zone, extra_data), move_asset_to=zone,
        )

    def _deliver(self, planning, asset, zone, extra_data=None):
        return run_strategy(
            "delivery", asset, self.employee, self.user, self._payload(planning, zone, extra_data),
        )

    def _vert(self, planning, asset):
        return run_strategy(
            "receipt", asset, self.employee, self.user,
            {
                "nro_albaran": "ALB-001",
                "foto_albaran": "",
                "peso_neto": "500",
                "material_vertido": "RCD Limpio",
                "zone_id": self.obra.id,
                "planning_id": planning.id,
            },
        )


class CubaOperationCompletionTests(CubaFlowTestCase):
    """Puesta / retirada / cambio close only when that operation's work is done."""

    def setUp(self):
        super().setUp()
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

    def test_cambio_empty_warehouse_pickup_does_not_close(self):
        planning = self._plan(CubaOperationType.CAMBIO)
        self._move(planning, self.new_cuba, self.almacen)
        planning.refresh_from_db()
        self.assertEqual(planning.status, PlanningStatus.IN_TRANSIT)

    def test_two_leg_operations_stay_open_after_a_warehouse_pickup_and_obra_dropoff(self):
        """Picking a cuba up at the almacén doesn't resolve the pickup leg
        (that happens at the destination obra), so dropping it at the obra
        alone leaves the plan open. For CAMBIO the owed leg is recoger la
        vieja; for CAMBIO_AGUA -- a same-asset round trip, see
        test_same_asset_round_trips_close_after_pickup_and_dropoff_at_the_obra
        -- it's picking that same cuba up at the obra."""
        for operation in (CubaOperationType.CAMBIO, CubaOperationType.CAMBIO_AGUA):
            with self.subTest(operation=operation):
                planning = self._plan(operation)
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

    def test_same_asset_round_trips_close_after_pickup_and_dropoff_at_the_obra(self):
        """CAMBIO_MISMA_CUBA and CAMBIO_AGUA pick up and drop off the very same
        cuba at the obra -- not a two-cuba swap, and with an empty pickup no
        verteo step in between. (A full CAMBIO_MISMA_CUBA pickup owes a verteo
        first: see CubaChecklistTests.test_cambio_misma_cuba_checklist_full_cycle.)"""
        for operation in (CubaOperationType.CAMBIO_MISMA_CUBA, CubaOperationType.CAMBIO_AGUA):
            with self.subTest(operation=operation):
                planning = self._plan(operation, asset=self.old_cuba)
                self._move(planning, self.old_cuba, self.obra)
                planning.refresh_from_db()
                self.assertEqual(planning.status, PlanningStatus.IN_TRANSIT)
                self._deliver(planning, self.old_cuba, self.obra)
                planning.refresh_from_db()
                self.assertEqual(planning.status, PlanningStatus.DELIVERED)
                self.old_cuba.refresh_from_db()
                self.assertEqual(self.old_cuba.current_transaction_status(), "DELIVERED")

    def test_next_task_hides_the_use_pending_cuba_shortcut_for_empty_truck_operations(self):
        """CAMBIO_MISMA_CUBA, CAMBIO_AGUA and RETIRADA all need the truck to
        arrive empty — the driver can't skip the pickup by reusing a cuba
        they're still carrying from a previous job."""
        planning = self._plan(CubaOperationType.RETIRADA, asset=self.old_cuba)
        self._move(planning, self.old_cuba, self.obra)
        client = APIClient()
        client.force_authenticate(user=self.user)
        for operation in (
            CubaOperationType.CAMBIO_MISMA_CUBA,
            CubaOperationType.CAMBIO_AGUA,
            CubaOperationType.RETIRADA,
        ):
            with self.subTest(operation=operation):
                next_plan = make_planning(
                    self.company, asset=None, assigned_to=self.employee,
                    cuba_operation_type=operation, destination=self.almacen,
                )
                body = client.get("/en/api/plannings/next/").json()
                self.assertEqual(body["task"]["id"], next_plan.id)
                self.assertIsNotNone(body["pending_dropoff"])
                self.assertFalse(body["pending_dropoff"]["can_use_for_next_task"])
                next_plan.delete()

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


class CubaChecklistTests(CubaFlowTestCase):
    """AssetPlanning.cuba_checklist() — the progress steps behind the San Carlos
    app's checkpoint stepper."""

    def setUp(self):
        super().setUp()
        self.cuba = make_asset(self.company, self.asset_type, employee=self.employee, zone=self.obra, name="CUBA-1")

    def _plan(self, operation, **overrides):
        return make_planning(
            self.company, asset=self.cuba, assigned_to=self.employee,
            cuba_operation_type=operation, destination=self.obra, **overrides,
        )

    def _statuses(self, planning):
        planning.refresh_from_db()
        return {s["key"]: s["status"] for s in planning.cuba_checklist()}

    def test_puesta_checklist_starts_with_uncollected_cuba(self):
        planning = self._plan(CubaOperationType.PUESTA)
        self.assertEqual(
            self._statuses(planning),
            {"recoger_cuba": "pending", "dejar": "pending"},
        )
        self._deliver(planning, self.cuba, self.obra)
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
        self._move(planning, self.cuba, self.obra)
        self.assertEqual(
            self._statuses(planning),
            {"recoger_cuba": "done", "recoger": "done", "verter": "skipped"},
        )

    def test_cambio_misma_cuba_checklist_full_cycle(self):
        """A full pickup owes a verteo before the dropoff can close the plan;
        the checklist steps and the planning status move together."""
        planning = self._plan(CubaOperationType.CAMBIO_MISMA_CUBA)
        self.assertEqual(
            [s["key"] for s in planning.cuba_checklist()],
            ["recoger_cuba", "recoger", "verter", "dejar"],
        )
        self.assertEqual(
            self._statuses(planning),
            {"recoger_cuba": "pending", "recoger": "pending", "verter": "pending", "dejar": "pending"},
        )
        self._move(planning, self.cuba, self.obra, extra_data={CubaTransactionKey.ESTADO: CubaFillState.FULL})
        self.assertEqual(
            self._statuses(planning),
            {"recoger_cuba": "done", "recoger": "done", "verter": "pending", "dejar": "pending"},
        )
        self.assertEqual(planning.status, PlanningStatus.IN_TRANSIT)
        self._vert(planning, self.cuba)
        self.assertEqual(
            self._statuses(planning),
            {"recoger_cuba": "done", "recoger": "done", "verter": "done", "dejar": "pending"},
        )
        self.assertEqual(planning.status, PlanningStatus.IN_TRANSIT)
        self._deliver(planning, self.cuba, self.obra)
        self.assertEqual(
            self._statuses(planning),
            {"recoger_cuba": "done", "recoger": "done", "verter": "done", "dejar": "done"},
        )
        self.assertEqual(planning.status, PlanningStatus.DELIVERED)
        self.cuba.refresh_from_db()
        self.assertEqual(self.cuba.current_transaction_status(), "DELIVERED")

    def test_cambio_agua_checklist_has_no_verter_step(self):
        planning = self._plan(CubaOperationType.CAMBIO_AGUA)
        self.assertEqual(
            self._statuses(planning),
            {"recoger_cuba": "pending", "recoger": "pending", "dejar": "pending"},
        )
        self._move(planning, self.cuba, self.obra)
        self._deliver(planning, self.cuba, self.obra)
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
        self._deliver(planning, self.cuba, self.obra)
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
        self._move(planning, self.cuba, self.obra)
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
        """A full retirada pickup owes a verteo: the checklist's verter step
        stays pending and the plan is coloured VERTEO_PENDING until it's done."""
        from assets.models import VERTEO_PENDING_COLOR, STATUS_COLORS

        planning = self._plan(CubaOperationType.RETIRADA)
        self.assertEqual(planning.status_color, STATUS_COLORS[PlanningStatus.PENDING])
        self._move(planning, self.cuba, self.obra, extra_data={CubaTransactionKey.ESTADO: CubaFillState.FULL})
        planning.refresh_from_db()
        self.assertEqual(planning.status, PlanningStatus.IN_TRANSIT)
        self.assertTrue(planning.has_pending_verteo())
        self.assertEqual(planning.status_color, VERTEO_PENDING_COLOR)
        self.assertEqual(
            self._statuses(planning),
            {"recoger_cuba": "done", "recoger": "done", "verter": "pending"},
        )
        self._vert(planning, self.cuba)
        planning.refresh_from_db()
        self.assertEqual(planning.status, PlanningStatus.DELIVERED)
        self.assertFalse(planning.has_pending_verteo())
        self.assertEqual(planning.status_color, STATUS_COLORS[PlanningStatus.DELIVERED])
        self.assertEqual(
            self._statuses(planning),
            {"recoger_cuba": "done", "recoger": "done", "verter": "done"},
        )

    def test_status_color_stays_plain_in_transit_for_an_empty_pickup_still_awaiting_dropoff(self):
        from assets.models import STATUS_COLORS

        planning = self._plan(CubaOperationType.CAMBIO_MISMA_CUBA)
        self._move(planning, self.cuba, self.obra)
        planning.refresh_from_db()
        self.assertEqual(planning.status, PlanningStatus.IN_TRANSIT)
        self.assertFalse(planning.has_pending_verteo())
        self.assertEqual(planning.status_color, STATUS_COLORS[PlanningStatus.IN_TRANSIT])

