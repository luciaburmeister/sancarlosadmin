"""
assets/tests/test_manual_transaction_service.py

Covers assets.services.manual_transaction.create_manual_transaction -- the
admin "Create Transaction" wizard's manual-correction path for when the
mobile app's automatic flow (MovementStrategy/DeliveryStrategy) never ran.
"""
from unittest.mock import patch

from django.test import TestCase

from api.fleet.tests.helpers import make_asset, make_asset_type, make_company, make_employee, make_planning, make_user, make_zone
from assets.models import (
    AssetIdentifier,
    AssetIdentifierLabel,
    AssetTransaction,
    CubaFillState,
    CubaTransactionKey,
    CubaWorkType,
    PlanningStatus,
    PorteTransactionDataKey,
    PlanningType,
    TransactionTypeChoices,
)
from assets.services.manual_transaction import create_manual_transaction
from company.models import WorkingZone
from management.models import ProjectFile
from sales.models import Client


class CreateManualTransactionMovementTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = make_company()
        cls.user = make_user()
        cls.employee = make_employee(cls.company, cls.user)
        cls.asset_type = make_asset_type(cls.company)
        cls.client_obj = Client.objects.create(company=cls.company, name="Test Client")
        cls.project_file = ProjectFile.objects.create(
            company=cls.company, client=cls.client_obj, name="Test Expediente",
        )
        cls.zone = make_zone(cls.company, name="Obra")
        cls.zone.project_file = cls.project_file
        cls.zone.save()
        cls.asset = make_asset(cls.company, cls.asset_type, employee=cls.employee, zone=cls.zone)

    def setUp(self):
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

    def test_movement_of_a_full_cuba_puts_the_planning_in_transit(self):
        # Proves the manual path calls sync_cuba_operation_status like the
        # mobile flow does. A full-cuba pickup is only resolved once its verteo
        # is registered, so the planning leaves PENDING for IN_TRANSIT (an
        # empty pickup would resolve itself straight to DELIVERED).
        create_manual_transaction(
            planning=self.planning, transaction_type=TransactionTypeChoices.MOVEMENT,
            data={CubaTransactionKey.ESTADO: CubaFillState.FULL},
        )
        self.planning.refresh_from_db()
        self.assertEqual(self.planning.status, PlanningStatus.IN_TRANSIT)

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
        self.assertEqual(transaction.data["tipo_cuba"], "Tierras")
        self.assertEqual(transaction.data["cubaEstado"], "Cuba llena")
        self.assertEqual(transaction.data[CubaTransactionKey.WORK], CubaWorkType.PICKUP)

    def test_movement_copies_m3_and_plate_already_on_the_planning(self):
        truck = make_asset(self.company, self.asset_type, name="Truck-001")
        AssetIdentifier.objects.create(asset=truck, label=AssetIdentifierLabel.PLATE, value="0709CBB")
        self.planning.cuba_size = "7m3"
        self.planning.transport_asset = truck
        self.planning.save(update_fields=["cuba_size", "transport_asset"])
        transaction = create_manual_transaction(
            planning=self.planning, transaction_type=TransactionTypeChoices.MOVEMENT,
        )
        self.assertEqual(transaction.data[CubaTransactionKey.M3], "7")
        self.assertEqual(transaction.data[PorteTransactionDataKey.MATRICULA], "0709CBB")

    def test_movement_m3_uses_the_container_not_the_size_the_plan_asked_for(self):
        # The app writes the cuba's own capacity ("12"). The plan's requested
        # size ("7m3") is often a different container and must not win.
        self.asset.data = {"capacidad": "12"}
        self.asset.save(update_fields=["data"])
        self.planning.cuba_size = "7m3"
        self.planning.save(update_fields=["cuba_size"])
        transaction = create_manual_transaction(
            planning=self.planning, transaction_type=TransactionTypeChoices.MOVEMENT,
        )
        self.assertEqual(transaction.data[CubaTransactionKey.M3], "12")

    def test_movement_copies_truck_type_and_the_site_address(self):
        truck = make_asset(self.company, self.asset_type, name="Truck-002")
        truck.data = {"tipo_camion": "Volquete", "implemento": "Gancho"}
        truck.save(update_fields=["data"])
        AssetIdentifier.objects.create(asset=truck, label=AssetIdentifierLabel.PLATE, value="5193CHM")
        self.planning.transport_asset = truck
        self.planning.save(update_fields=["transport_asset"])
        self.zone.street_address = "CALLE Torre de san telmo"
        self.zone.city = "MALAGA"
        self.zone.postal_code = "29008"
        self.zone.save(update_fields=["street_address", "city", "postal_code"])
        transaction = create_manual_transaction(
            planning=self.planning,
            transaction_type=TransactionTypeChoices.MOVEMENT,
            notes="Dejar en la puerta",
        )
        self.assertEqual(transaction.data[CubaTransactionKey.TRUCK_TYPE], "Volquete")
        self.assertEqual(transaction.data[CubaTransactionKey.IMPLEMENT], "Gancho")
        self.assertEqual(transaction.data[CubaTransactionKey.SITE], "CALLE Torre de san telmo, MALAGA, 29008")
        self.assertEqual(transaction.data[CubaTransactionKey.OBSERVATIONS], "Dejar en la puerta")

    def test_movement_copies_the_zone_client_and_type(self):
        self.zone.client = self.client_obj
        self.zone.save(update_fields=["client"])
        try:
            transaction = create_manual_transaction(
                planning=self.planning, transaction_type=TransactionTypeChoices.MOVEMENT,
            )
        finally:
            self.zone.client = None
            self.zone.save(update_fields=["client"])
        self.assertEqual(transaction.data[CubaTransactionKey.CLIENT_NAME], "Test Client")
        self.assertEqual(transaction.data[CubaTransactionKey.ZONE_TYPE], WorkingZone.ZoneType.OBRA)
        self.assertEqual(transaction.data[PorteTransactionDataKey.PLANNING_ID], self.planning.pk)

    def test_movement_defaults_to_empty_data(self):
        transaction = create_manual_transaction(
            planning=self.planning, transaction_type=TransactionTypeChoices.MOVEMENT,
        )
        self.assertEqual(transaction.data, {
            CubaTransactionKey.WORK: CubaWorkType.PICKUP,
            PorteTransactionDataKey.PLANNING_ID: self.planning.pk,
            CubaTransactionKey.CLIENT_NAME: "",
            CubaTransactionKey.ZONE_TYPE: WorkingZone.ZoneType.OBRA,
            CubaTransactionKey.SITE: "Calle Test 1, Madrid, 28001",
        })

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
    @classmethod
    def setUpTestData(cls):
        cls.company = make_company()
        cls.user = make_user()
        cls.employee = make_employee(cls.company, cls.user)
        cls.asset_type = make_asset_type(cls.company)
        cls.client_obj = Client.objects.create(company=cls.company, name="Test Client")
        cls.project_file_a = ProjectFile.objects.create(
            company=cls.company, client=cls.client_obj, name="Origin Expediente",
        )
        cls.project_file_b = ProjectFile.objects.create(
            company=cls.company, client=cls.client_obj, name="Destination Expediente",
        )
        cls.origin_zone = make_zone(cls.company, name="Origin")
        cls.origin_zone.project_file = cls.project_file_a
        cls.origin_zone.save()
        cls.dest_zone = make_zone(cls.company, name="Destination")
        cls.dest_zone.project_file = cls.project_file_b
        cls.dest_zone.save()

    def setUp(self):
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
        self.assertEqual(transaction.data[CubaTransactionKey.CLIENT_NAME], "")
        self.assertEqual(transaction.data[CubaTransactionKey.ZONE_TYPE], WorkingZone.ZoneType.OBRA)
        self.assertEqual(transaction.data[CubaTransactionKey.SITE], self.dest_zone.full_address)
        self.assertEqual(transaction.data[PorteTransactionDataKey.PLANNING_ID], self.planning.pk)

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
    @classmethod
    def setUpTestData(cls):
        cls.company = make_company()
        cls.user = make_user()
        cls.employee = make_employee(cls.company, cls.user)
        cls.asset_type = make_asset_type(cls.company)
        cls.client_obj = Client.objects.create(company=cls.company, name="Test Client")
        cls.project_file = ProjectFile.objects.create(
            company=cls.company, client=cls.client_obj, name="Test Expediente",
        )
        cls.zone = make_zone(cls.company, name="Obra")
        cls.zone.project_file = cls.project_file
        cls.zone.save()

    def setUp(self):
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
        self.assertEqual(transaction.data[PorteTransactionDataKey.PLANNING_ID], self.planning.pk)

    def test_receipt_saves_net_weight_when_given(self):
        transaction = create_manual_transaction(
            planning=self.planning,
            transaction_type=TransactionTypeChoices.RECEIPT,
            data={"nro_albaran": "01083485", "material_vertido": "Tierras", "peso_neto": "500"},
        )
        self.assertEqual(transaction.receipts.get().data["peso_neto"], "500")

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
        self.assertEqual(
            implicit_pickups.get().data[PorteTransactionDataKey.PLANNING_ID], self.planning.pk,
        )

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

    @classmethod
    def setUpTestData(cls):
        cls.company = make_company()
        cls.user = make_user()
        cls.employee = make_employee(cls.company, cls.user)
        cls.asset_type = make_asset_type(cls.company)
        cls.client_obj = Client.objects.create(company=cls.company, name="Test Client")
        cls.origin_zone = make_zone(cls.company, name="Origen")
        cls.dest_zone = make_zone(cls.company, name="Destino")
        cls.dest_project_file = ProjectFile.objects.create(
            company=cls.company, client=cls.client_obj, name="Destination Expediente",
        )
        cls.dest_zone.project_file = cls.dest_project_file
        cls.dest_zone.save()
        cls.truck = make_asset(cls.company, cls.asset_type, name="Truck-001")

    def setUp(self):
        self.planning = make_planning(
            self.company, assigned_to=self.employee, planning_type=PlanningType.PORTE,
            status=PlanningStatus.PENDING, origin=self.origin_zone, destination=self.dest_zone,
            transport_asset=self.truck,
        )

    def test_porte_movement_uses_planning_zones_and_transport_asset(self):
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
        self.assertEqual(transaction.data[PorteTransactionDataKey.PLANNING_ID], self.planning.pk)
        self.assertIsNone(transaction.data[PorteTransactionDataKey.CLIENT_ID])

    def test_porte_copies_load_and_machine_from_the_planning(self):
        machine = make_asset(self.company, self.asset_type, name="Machine-001")
        self.planning.load_description = "zahorra"
        self.planning.machine = machine
        self.planning.save(update_fields=["load_description", "machine"])
        transaction = create_manual_transaction(
            planning=self.planning, transaction_type=TransactionTypeChoices.PORTE_LOAD,
        )
        self.assertEqual(transaction.data["load_description"], "zahorra")
        self.assertEqual(transaction.data["machine_id"], machine.pk)

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
