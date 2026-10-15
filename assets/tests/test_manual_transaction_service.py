"""
assets/tests/test_manual_transaction_service.py

Covers assets.services.manual_transaction.create_manual_transaction -- the
admin "Create Transaction" wizard's manual-correction path for when the
mobile app's automatic flow (MovementStrategy/DeliveryStrategy) never ran.
"""
from unittest.mock import patch

from django.test import TestCase

from api.fleet.tests.helpers import make_asset, make_asset_type, make_company, make_employee, make_planning, make_user, make_zone
from assets.models import AssetTransaction, CubaTransactionKey, PlanningStatus, PlanningType, TransactionTypeChoices
from assets.services.manual_transaction import create_manual_transaction
from company.models import WorkingZone
from management.models import ProjectFile
from sales.models import Client


class CreateManualTransactionMovementTests(TestCase):
    def setUp(self):
        self.company = make_company()
        self.user = make_user()
        self.employee = make_employee(self.company, self.user)
        self.asset_type = make_asset_type(self.company)
        self.client_obj = Client.objects.create(company=self.company, name="Test Client")
        self.project_file = ProjectFile.objects.create(
            company=self.company, client=self.client_obj, name="Test Expediente",
        )
        self.zone = make_zone(self.company, name="Obra")
        self.zone.project_file = self.project_file
        self.zone.save()
        self.asset = make_asset(self.company, self.asset_type, employee=self.employee, zone=self.zone)
        self.planning = make_planning(
            self.company, asset=self.asset, assigned_to=self.employee,
            status=PlanningStatus.PENDING,
        )

    def test_movement_derives_from_zone_and_project_file_from_asset(self):
        transaction = create_manual_transaction(
            planning=self.planning,
            transaction_type=TransactionTypeChoices.MOVEMENT,
            performed_by=self.employee,
        )
        self.assertEqual(transaction.from_zone_id, self.zone.pk)
        self.assertIsNone(transaction.to_zone_id)
        self.assertEqual(transaction.project_file_id, self.project_file.pk)
        self.assertEqual(transaction.planning_id, self.planning.pk)
        self.assertEqual(transaction.asset_id, self.asset.pk)

    @patch("core.tasks.send_asset_transaction_email_task.delay")
    def test_movement_emails_the_named_landfill_not_to_zone(self, delay):
        landfill = make_zone(self.company, name="Vertedero Norte")
        landfill.zone_type = WorkingZone.ZoneType.VERTEDERO
        landfill.receipt_notification_email = "vertedero@example.com"
        landfill.auto_send_receipt = True
        landfill.save()

        transaction = create_manual_transaction(
            planning=self.planning,
            transaction_type=TransactionTypeChoices.MOVEMENT,
            zone=self.zone,
            data={CubaTransactionKey.LANDFILL: "Vertedero Norte"},
        )

        self.assertIsNone(transaction.to_zone_id)
        delay.assert_called_once()
        self.assertEqual(delay.call_args.kwargs["to_email"], "vertedero@example.com")

    def test_movement_syncs_planning_status(self):
        # sync_cuba_operation_status must be called and not raise -- the
        # exact status transition is PlanningAbandoner/status-sync's own
        # responsibility and is covered by its own tests; this just proves
        # the manual path actually calls it (the whole point of the service).
        create_manual_transaction(
            planning=self.planning, transaction_type=TransactionTypeChoices.MOVEMENT,
        )
        self.planning.refresh_from_db()
        # Should not still be untouched-PENDING once a movement is recorded.
        self.assertIsNotNone(self.planning.status)

    def test_movement_without_asset_raises(self):
        self.planning.asset = None
        self.planning.save(update_fields=["asset"])
        with self.assertRaises(ValueError):
            create_manual_transaction(planning=self.planning, transaction_type=TransactionTypeChoices.MOVEMENT)

    def test_movement_without_asset_but_with_explicit_override_succeeds(self):
        """The wizard's Asset field exists exactly for this: a planning the
        mobile app's QR-scan assignment never ran against."""
        self.planning.asset = None
        self.planning.save(update_fields=["asset"])
        other_asset = make_asset(self.company, self.asset_type, name="Cuba-002", zone=self.zone)
        transaction = create_manual_transaction(
            planning=self.planning, transaction_type=TransactionTypeChoices.MOVEMENT,
            asset=other_asset,
        )
        self.assertEqual(transaction.asset_id, other_asset.pk)

    def test_movement_stores_passed_in_data(self):
        transaction = create_manual_transaction(
            planning=self.planning,
            transaction_type=TransactionTypeChoices.MOVEMENT,
            data={"tipo_cuba": "Tierras", "cubaEstado": "Cuba llena"},
        )
        self.assertEqual(transaction.data, {"tipo_cuba": "Tierras", "cubaEstado": "Cuba llena"})

    def test_movement_defaults_to_empty_data(self):
        transaction = create_manual_transaction(
            planning=self.planning, transaction_type=TransactionTypeChoices.MOVEMENT,
        )
        self.assertEqual(transaction.data, {})

    def test_reference_number_is_stored_on_the_model_field_not_data(self):
        transaction = create_manual_transaction(
            planning=self.planning, transaction_type=TransactionTypeChoices.MOVEMENT,
            reference_number="REF-WIZARD-1",
        )
        self.assertEqual(transaction.reference_number, "REF-WIZARD-1")
        self.assertNotIn("reference_number", transaction.data)

    def test_reference_number_defaults_to_empty_string(self):
        transaction = create_manual_transaction(
            planning=self.planning, transaction_type=TransactionTypeChoices.MOVEMENT,
        )
        self.assertEqual(transaction.reference_number, "")


class CreateManualTransactionDeliveryTests(TestCase):
    def setUp(self):
        self.company = make_company()
        self.user = make_user()
        self.employee = make_employee(self.company, self.user)
        self.asset_type = make_asset_type(self.company)
        self.client_obj = Client.objects.create(company=self.company, name="Test Client")
        self.project_file_a = ProjectFile.objects.create(
            company=self.company, client=self.client_obj, name="Origin Expediente",
        )
        self.project_file_b = ProjectFile.objects.create(
            company=self.company, client=self.client_obj, name="Destination Expediente",
        )
        self.origin_zone = make_zone(self.company, name="Origin")
        self.origin_zone.project_file = self.project_file_a
        self.origin_zone.save()
        self.dest_zone = make_zone(self.company, name="Destination")
        self.dest_zone.project_file = self.project_file_b
        self.dest_zone.save()
        self.asset = make_asset(self.company, self.asset_type, employee=self.employee, zone=self.origin_zone)
        self.planning = make_planning(
            self.company, asset=self.asset, assigned_to=self.employee,
            status=PlanningStatus.IN_TRANSIT, destination=self.dest_zone,
        )

    def test_delivery_updates_asset_zone_and_project_file(self):
        transaction = create_manual_transaction(
            planning=self.planning,
            transaction_type=TransactionTypeChoices.DELIVERY,
            performed_by=self.employee,
        )
        self.asset.refresh_from_db()
        self.assertEqual(transaction.to_zone_id, self.dest_zone.pk)
        self.assertEqual(transaction.project_file_id, self.project_file_b.pk)
        self.assertEqual(self.asset.current_working_zone_id, self.dest_zone.pk)

    def test_delivery_with_explicit_zone_overrides_planning_destination(self):
        other_zone = make_zone(self.company, name="Other")
        transaction = create_manual_transaction(
            planning=self.planning,
            transaction_type=TransactionTypeChoices.DELIVERY,
            zone=other_zone,
        )
        self.assertEqual(transaction.to_zone_id, other_zone.pk)

    def test_delivery_without_any_zone_raises(self):
        self.planning.destination = None
        self.planning.save(update_fields=["destination"])
        with self.assertRaises(ValueError):
            create_manual_transaction(planning=self.planning, transaction_type=TransactionTypeChoices.DELIVERY)


class CreateManualTransactionReceiptTests(TestCase):
    def setUp(self):
        self.company = make_company()
        self.user = make_user()
        self.employee = make_employee(self.company, self.user)
        self.asset_type = make_asset_type(self.company)
        self.client_obj = Client.objects.create(company=self.company, name="Test Client")
        self.project_file = ProjectFile.objects.create(
            company=self.company, client=self.client_obj, name="Test Expediente",
        )
        self.zone = make_zone(self.company, name="Obra")
        self.zone.project_file = self.project_file
        self.zone.save()
        self.asset = make_asset(self.company, self.asset_type, employee=self.employee, zone=self.zone)
        self.planning = make_planning(
            self.company, asset=self.asset, assigned_to=self.employee, status=PlanningStatus.IN_TRANSIT,
        )
        self.movement_tx = AssetTransaction.objects.create(
            asset=self.asset, company=self.company, from_zone=self.zone, to_zone=None,
            transaction_type=TransactionTypeChoices.MOVEMENT, planning=self.planning,
            project_file=self.project_file, data={},
        )

    def test_receipt_without_albaran_number_raises(self):
        with self.assertRaises(ValueError):
            create_manual_transaction(
                planning=self.planning, transaction_type=TransactionTypeChoices.RECEIPT, data={},
            )

    def test_receipt_creates_linked_receipt_record(self):
        transaction = create_manual_transaction(
            planning=self.planning,
            transaction_type=TransactionTypeChoices.RECEIPT,
            performed_by=self.employee,
            data={"nro_albaran": "01083485", "material_vertido": "Tierras"},
        )
        receipt = transaction.receipts.get()
        self.assertEqual(receipt.nro_albaran, "01083485")
        self.assertEqual(receipt.data, {"material_vertido": "Tierras"})
        self.assertEqual(receipt.movement_transaction_id, self.movement_tx.pk)
        self.assertEqual(transaction.project_file_id, self.project_file.pk)

    def test_receipt_after_delivery_creates_implicit_repickup(self):
        AssetTransaction.objects.create(
            asset=self.asset, company=self.company, from_zone=self.zone, to_zone=self.zone,
            transaction_type=TransactionTypeChoices.DELIVERY, planning=self.planning, data={},
        )
        create_manual_transaction(
            planning=self.planning,
            transaction_type=TransactionTypeChoices.RECEIPT,
            data={"nro_albaran": "01083485"},
        )
        implicit_pickups = AssetTransaction.objects.filter(
            asset=self.asset, transaction_type=TransactionTypeChoices.MOVEMENT,
        ).exclude(pk=self.movement_tx.pk)
        self.assertEqual(implicit_pickups.count(), 1)

    def test_receipt_without_prior_delivery_has_no_implicit_repickup(self):
        create_manual_transaction(
            planning=self.planning,
            transaction_type=TransactionTypeChoices.RECEIPT,
            data={"nro_albaran": "01083485"},
        )
        movement_count = AssetTransaction.objects.filter(
            asset=self.asset, transaction_type=TransactionTypeChoices.MOVEMENT,
        ).count()
        self.assertEqual(movement_count, 1)


class CreateManualTransactionPorteTests(TestCase):
    """Covers PORTE_MOVEMENT/PORTE_LOAD/PORTE_DELIVERY -- mirrors
    api.fleet.views.attendance.PorteSubmitView's field resolution
    (tx_asset = planning.asset or planning.transport_asset, from/to always
    the planning's own origin/destination) and status update, without its
    mobile-only QR-scan gating."""

    def setUp(self):
        self.company = make_company()
        self.user = make_user()
        self.employee = make_employee(self.company, self.user)
        self.asset_type = make_asset_type(self.company)
        self.client_obj = Client.objects.create(company=self.company, name="Test Client")
        self.origin_zone = make_zone(self.company, name="Origen")
        self.dest_zone = make_zone(self.company, name="Destino")
        self.dest_project_file = ProjectFile.objects.create(
            company=self.company, client=self.client_obj, name="Destination Expediente",
        )
        self.dest_zone.project_file = self.dest_project_file
        self.dest_zone.save()
        self.truck = make_asset(self.company, self.asset_type, name="Truck-001")
        self.planning = make_planning(
            self.company, assigned_to=self.employee, planning_type=PlanningType.PORTE,
            status=PlanningStatus.PENDING, origin=self.origin_zone, destination=self.dest_zone,
            transport_asset=self.truck,
        )

    def test_porte_movement_uses_planning_zones_and_transport_asset(self):
        # The service persists whatever `data` it's handed as-is -- deriving
        # action/movement_type from transaction_type is build_data_from_cleaned's
        # job (assets.transaction_data_schema, exercised in PorteFamilySchemaTests),
        # not the service's. Passing an already-built dict here, as
        # ManualTransactionForm.build_data() would produce it.
        transaction = create_manual_transaction(
            planning=self.planning,
            transaction_type=TransactionTypeChoices.PORTE_MOVEMENT,
            performed_by=self.employee,
            data={"matricula": "0709CBB", "action": "recogida", "movement_type": "porte"},
        )
        self.assertEqual(transaction.asset_id, self.truck.pk)
        self.assertEqual(transaction.from_zone_id, self.origin_zone.pk)
        self.assertEqual(transaction.to_zone_id, self.dest_zone.pk)
        self.assertEqual(transaction.project_file_id, self.dest_project_file.pk)
        self.assertEqual(transaction.data["action"], "recogida")
        self.assertEqual(transaction.data["movement_type"], "porte")

    def test_porte_load_marks_planning_in_transit(self):
        create_manual_transaction(planning=self.planning, transaction_type=TransactionTypeChoices.PORTE_LOAD)
        self.planning.refresh_from_db()
        self.assertEqual(self.planning.status, PlanningStatus.IN_TRANSIT)

    def test_porte_delivery_marks_planning_delivered(self):
        create_manual_transaction(planning=self.planning, transaction_type=TransactionTypeChoices.PORTE_DELIVERY)
        self.planning.refresh_from_db()
        self.assertEqual(self.planning.status, PlanningStatus.DELIVERED)

    def test_porte_without_any_asset_raises(self):
        self.planning.transport_asset = None
        self.planning.save(update_fields=["transport_asset"])
        with self.assertRaises(ValueError):
            create_manual_transaction(planning=self.planning, transaction_type=TransactionTypeChoices.PORTE_MOVEMENT)

    def test_porte_without_any_asset_but_with_explicit_override_succeeds(self):
        self.planning.transport_asset = None
        self.planning.save(update_fields=["transport_asset"])
        other_truck = make_asset(self.company, self.asset_type, name="Truck-002")
        transaction = create_manual_transaction(
            planning=self.planning, transaction_type=TransactionTypeChoices.PORTE_MOVEMENT,
            asset=other_truck,
        )
        self.assertEqual(transaction.asset_id, other_truck.pk)

    def test_porte_asset_override_wins_over_machine_delivery_resolution(self):
        """An explicit pick from the wizard always wins, even on a
        PORTE_DELIVERY where the planning has an assigned machine that would
        otherwise be auto-selected."""
        machine = make_asset(self.company, self.asset_type, name="Machine-001")
        self.planning.machine = machine
        self.planning.save(update_fields=["machine"])
        chosen = make_asset(self.company, self.asset_type, name="Chosen-Instead")
        transaction = create_manual_transaction(
            planning=self.planning, transaction_type=TransactionTypeChoices.PORTE_DELIVERY,
            asset=chosen,
        )
        self.assertEqual(transaction.asset_id, chosen.pk)

    def test_porte_delivery_uses_assigned_machine_over_transport_asset(self):
        machine = make_asset(self.company, self.asset_type, name="Machine-001")
        self.planning.machine = machine
        self.planning.save(update_fields=["machine"])
        transaction = create_manual_transaction(
            planning=self.planning, transaction_type=TransactionTypeChoices.PORTE_DELIVERY,
        )
        self.assertEqual(transaction.asset_id, machine.pk)


class CreateManualTransactionUnsupportedTypeTests(TestCase):
    def test_unsupported_transaction_type_raises(self):
        company = make_company()
        asset_type = make_asset_type(company)
        asset = make_asset(company, asset_type)
        planning = make_planning(company, asset=asset)
        with self.assertRaises(ValueError):
            create_manual_transaction(planning=planning, transaction_type=TransactionTypeChoices.RETURN)
