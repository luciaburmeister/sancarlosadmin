"""
api/fleet/tests/test_portes.py

Porte and cuba planning list endpoints

Run just this file:
    python manage.py test api.fleet.tests.test_portes
"""

from datetime import date, time

from django.test import TestCase

from assets.models import (
    Asset, AssetPlanning, AssetTypeCategory, TransactionTypeChoices,
)
from api.fleet.tests.helpers import (
    ApiFixtureMixin,
    make_asset,
    make_asset_type,
    make_zone,
)

# 1x1 transparent PNG, used wherever the submit endpoint needs a base64 image.
TINY_PNG_B64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="


class PorteTestCase(ApiFixtureMixin, TestCase):
    """Authenticated driver plus the porte's Origen/Destino zones."""

    def setUp(self):
        super().setUp()
        self.origin = make_zone(self.company, name="Origen")
        self.destination = make_zone(self.company, name="Destino")

    def _submit(self, planning, action, extra=None, image=""):
        """POST /portes/<id>/submit/ for one step (recogida/carga/entrega);
        `image` fills both the firma and imagen base64 fields."""
        data = {"action": action, "firma": image, "imagen": image, "matricula": "1234ABC", "notes": ""}
        if extra:
            data.update(extra)
        return self.api_client.post(f"/en/api/portes/{planning.id}/submit/", data=data, format="json")


class PortePlanningTests(PorteTestCase):
    """
    Tests for the porte and cuba planning list endpoints.
    Portes and cubas are separate planning types and must not leak into each other.
    """

    def setUp(self):
        super().setUp()
        self.asset_type = make_asset_type(self.company)
        self.asset = make_asset(self.company, self.asset_type, employee=self.employee)

    def _make_planning(self, planning_type="porte", status="pending"):
        return AssetPlanning.objects.create(
            company=self.company,
            asset=self.asset,
            origin=self.origin,
            destination=self.destination,
            assigned_to=self.employee,
            scheduled_date=date.today(),
            scheduled_time=time(8, 0),
            planning_type=planning_type,
            status=status,
        )

    def test_porte_planning_appears_in_portes_list(self):
        """A porte planning must appear in GET /en/api/portes/"""
        self._make_planning(planning_type="porte")
        response = self.api_client.get("/en/api/portes/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()["transport_plannings"]), 1)

    def test_cuba_planning_appears_in_plannings_list(self):
        """A cuba planning must appear in GET /en/api/plannings/"""
        self._make_planning(planning_type="cuba")
        response = self.api_client.get("/en/api/plannings/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()["plannings"]), 1)

    def test_porte_planning_does_not_appear_in_cuba_list(self):
        """A porte planning must NOT appear in the cuba plannings list."""
        self._make_planning(planning_type="porte")
        response = self.api_client.get("/en/api/plannings/")
        self.assertEqual(len(response.json()["plannings"]), 0)

    def test_cuba_planning_does_not_appear_in_portes_list(self):
        """A cuba planning must NOT appear in the portes list."""
        self._make_planning(planning_type="cuba")
        response = self.api_client.get("/en/api/portes/")
        self.assertEqual(len(response.json()["transport_plannings"]), 0)

    def test_delivered_porte_does_not_appear_in_list(self):
        """
        A porte with status='delivered' must not appear in the active list.
        Only pending and in_transit plannings should show.
        """
        self._make_planning(planning_type="porte", status="delivered")
        response = self.api_client.get("/en/api/portes/")
        self.assertEqual(len(response.json()["transport_plannings"]), 0)


class PorteWithoutAssetTests(PorteTestCase):
    """
    Regression tests for service types with no cargo asset (Grúa, Material
    Externo) — `AssetPlanning.asset` is legitimately None for these since
    `asset` became optional (2026-08-04). Covers the two production bugs this
    caused: GET /portes/ 500ing for the whole driver (unguarded p.asset.id/
    .name), and POST /portes/<id>/submit/ 500ing on planning.asset.company —
    plus the truck-QR fallback and its same-truck-same-day dedup.
    """

    def setUp(self):
        super().setUp()
        self.truck_type = make_asset_type(self.company, name="Camión", category=AssetTypeCategory.TRUCK)
        self.truck = Asset.objects.create(
            company=self.company, asset_type=self.truck_type, name="MERCEDES 0709CBB",
        )

    def _make_planning(self, *, transport_asset=None, scheduled_time=time(8, 0), status="pending"):
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

    def test_list_falls_back_to_transport_asset_when_asset_is_none(self):
        """The bug from the screenshots: GET /portes/ must not 500 when a planning
        has no asset. With a truck assigned, asset_id/asset_name must be the
        truck's, so scanning it still works."""
        self._make_planning(transport_asset=self.truck)
        response = self.api_client.get("/en/api/portes/")
        self.assertEqual(response.status_code, 200)
        items = response.json()["transport_plannings"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["asset_id"], self.truck.id)
        self.assertEqual(items[0]["asset_name"], self.truck.name)

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
        response = self._submit(planning, "recogida", image=TINY_PNG_B64)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json().get("success"))
        planning.refresh_from_db()
        self.assertEqual(planning.status, "in_transit")
        tx = AssetTransaction.objects.get(data__planning_id=planning.id)
        self.assertEqual(tx.asset_id, self.truck.id)
        # The new FK is populated alongside the existing soft data["planning_id"].
        self.assertEqual(tx.planning_id, planning.id)
        # No reference_number sent -> the model field stays empty, not null.
        self.assertEqual(tx.reference_number, "")

    def test_submit_uses_selected_cargo_over_transport(self):
        """A specifically chosen cargo asset must survive save() and be used for the transaction, not the truck."""
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
        self.assertEqual(planning.asset_id, cargo.id)
        response = self._submit(planning, "recogida", image=TINY_PNG_B64)
        self.assertEqual(response.status_code, 200)
        tx = AssetTransaction.objects.get(data__planning_id=planning.id)
        self.assertEqual(tx.asset_id, cargo.id)

    def test_submit_stores_reference_number(self):
        from assets.models import AssetTransaction

        planning = self._make_planning(transport_asset=self.truck)
        response = self._submit(planning, "recogida", extra={"reference_number": "REF-12345"}, image=TINY_PNG_B64)
        self.assertEqual(response.status_code, 200)
        tx = AssetTransaction.objects.get(data__planning_id=planning.id)
        self.assertEqual(tx.reference_number, "REF-12345")
        self.assertNotIn("reference_number", tx.data)

    def test_submit_uses_the_planning_project_file_over_the_destination_zone(self):
        from assets.models import AssetTransaction
        from management.models import ProjectFile
        from sales.models import Client

        client = Client.objects.create(name="Client 1", company=self.company)
        zone_file = ProjectFile.objects.create(name="Zone", client=client, company=self.company)
        plan_file = ProjectFile.objects.create(name="Plan", client=client, company=self.company)
        self.destination.project_file = zone_file
        self.destination.save(update_fields=["project_file"])
        planning = self._make_planning(transport_asset=self.truck)
        planning.project_file = plan_file
        planning.save(update_fields=["project_file"])

        response = self._submit(planning, "recogida", image=TINY_PNG_B64)
        self.assertEqual(response.status_code, 200)
        tx = AssetTransaction.objects.get(planning=planning)
        self.assertEqual(tx.project_file_id, plan_file.pk)

    def test_submit_falls_back_to_the_destination_zone_when_the_plan_has_no_project_file(self):
        from assets.models import AssetTransaction
        from management.models import ProjectFile
        from sales.models import Client

        client = Client.objects.create(name="Client 1", company=self.company)
        zone_file = ProjectFile.objects.create(name="Zone", client=client, company=self.company)
        self.destination.project_file = zone_file
        self.destination.save(update_fields=["project_file"])
        planning = self._make_planning(transport_asset=self.truck)

        response = self._submit(planning, "recogida", image=TINY_PNG_B64)
        self.assertEqual(response.status_code, 200)
        tx = AssetTransaction.objects.get(planning=planning)
        self.assertEqual(tx.project_file_id, zone_file.pk)


class PorteCargaAndMachineTests(PorteTestCase):
    """New 'carga' step + AssetPlanning.machine (own-machine transport carried
    inside a cuba, tracked separately from the cuba itself)."""

    def setUp(self):
        super().setUp()
        self.cuba_type = make_asset_type(self.company, name="Cuba")
        self.cuba = make_asset(self.company, self.cuba_type, employee=self.employee, name="CUBA-PORTE")
        self.machine_type = make_asset_type(self.company, name="Retroexcavadora", category=AssetTypeCategory.MACHINE)
        self.machine = Asset.objects.create(company=self.company, asset_type=self.machine_type, name="MAQUINA-1")

    def _make_planning(self, *, service_type="porte_material", machine=None, asset=None):
        return AssetPlanning.objects.create(
            company=self.company,
            asset=asset if asset is not None else self.cuba,
            machine=machine,
            origin=self.origin,
            destination=self.destination,
            assigned_to=self.employee,
            scheduled_date=date.today(),
            planning_type="porte",
            service_type=service_type,
            status="in_transit",
        )

    def test_material_carga_is_a_plain_button_no_scan_needed(self):
        from assets.models import AssetTransaction, TransactionTypeChoices

        planning = self._make_planning(service_type="porte_material")
        response = self._submit(planning, "carga")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(
            AssetTransaction.objects.filter(
                planning=planning, transaction_type=TransactionTypeChoices.PORTE_LOAD,
            ).exists()
        )
        planning.refresh_from_db()
        self.assertEqual(planning.status, "in_transit")

    def test_machine_carga_requires_a_qr_scan(self):
        planning = self._make_planning(service_type="porte_maquina")
        response = self._submit(planning, "carga")
        self.assertEqual(response.status_code, 400)
        self.assertIn("escanear", response.json()["error"])

    def test_machine_carga_associates_the_scanned_machine(self):
        planning = self._make_planning(service_type="porte_maquina")
        response = self._submit(planning, "carga", extra={"asset_id": self.machine.id})
        self.assertEqual(response.status_code, 200)
        planning.refresh_from_db()
        self.assertEqual(planning.machine_id, self.machine.id)

    def test_machine_carga_rejects_a_non_machine_asset(self):
        planning = self._make_planning(service_type="porte_maquina")
        response = self._submit(planning, "carga", extra={"asset_id": self.cuba.id})
        self.assertEqual(response.status_code, 400)
        planning.refresh_from_db()
        self.assertIsNone(planning.machine_id)

    def test_entrega_blocked_for_machine_porte_without_carga_scan(self):
        planning = self._make_planning(service_type="porte_maquina", machine=self.machine)
        response = self._submit(planning, "entrega")
        self.assertEqual(response.status_code, 400)
        self.assertIn("máquina", response.json()["error"])

    def test_machine_carga_must_match_dispatch_assignment(self):
        other_machine = Asset.objects.create(
            company=self.company, asset_type=self.machine_type, name="MAQUINA-2",
        )
        planning = self._make_planning(service_type="porte_maquina", machine=self.machine)
        response = self._submit(planning, "carga", extra={"asset_id": other_machine.id})
        self.assertEqual(response.status_code, 400)
        self.assertIn("coincide", response.json()["error"])

    def test_machine_carga_accepts_matching_dispatch_assignment(self):
        planning = self._make_planning(service_type="porte_maquina", machine=self.machine)
        response = self._submit(planning, "carga", extra={"asset_id": self.machine.id})
        self.assertEqual(response.status_code, 200)

    def test_entrega_after_carga_scan_completes_the_machine_porte(self):
        """The machine's zone sync on PORTE_DELIVERY is the model's job —
        assets/tests/test_zone_sync.py (PorteDeliveryZoneSyncTests) covers it."""
        planning = self._make_planning(service_type="porte_maquina", machine=self.machine)
        self._submit(planning, "carga", extra={"asset_id": self.machine.id})
        response = self._submit(planning, "entrega")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json().get("success"))
        planning.refresh_from_db()
        self.assertEqual(planning.status, "delivered")

    def test_entrega_does_not_change_cuba_status_for_machine_porte(self):
        planning = self._make_planning(service_type="porte_maquina", machine=self.machine)
        self._submit(planning, "recogida")
        self._submit(planning, "carga", extra={"asset_id": self.machine.id})
        response = self._submit(planning, "entrega")
        self.assertEqual(response.status_code, 200)
        self.cuba.refresh_from_db()
        self.assertEqual(self.cuba.current_transaction_status(), "PICKED_UP")
        self.assertFalse(
            self.cuba.transactions.filter(
                transaction_type=TransactionTypeChoices.PORTE_DELIVERY,
            ).exists()
        )

    def test_entrega_completes_a_material_porte_without_a_machine(self):
        """A material porte has no machine to scan: entrega goes straight
        through. (That its cuba's zone is left alone is
        PorteDeliveryZoneSyncTests.test_delivery_for_other_service_type_does_not_sync.)"""
        planning = self._make_planning(service_type="porte_material")
        response = self._submit(planning, "entrega")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json().get("success"))
        planning.refresh_from_db()
        self.assertEqual(planning.status, "delivered")


class PorteMultiMachineTests(PorteTestCase):
    """A planning carrying several machines, each individually scanned,
    loaded and delivered -- possibly at different destinations."""

    def setUp(self):
        super().setUp()
        self.cuba_type = make_asset_type(self.company, name="Cuba")
        self.cuba = make_asset(self.company, self.cuba_type, employee=self.employee, name="CUBA-PORTE")
        self.machine_type = make_asset_type(self.company, name="Retroexcavadora", category=AssetTypeCategory.MACHINE)
        self.machine = Asset.objects.create(company=self.company, asset_type=self.machine_type, name="MAQUINA-1")
        self.other_machine = Asset.objects.create(
            company=self.company, asset_type=self.machine_type, name="MAQUINA-2",
        )
        self.other_destination = make_zone(self.company, name="Otro Destino")

    def _make_planning(self, *, service_type="porte_maquina"):
        return AssetPlanning.objects.create(
            company=self.company,
            asset=self.cuba,
            origin=self.origin,
            destination=self.destination,
            assigned_to=self.employee,
            scheduled_date=date.today(),
            planning_type="porte",
            service_type=service_type,
            status="in_transit",
        )

    def test_scanning_an_undefined_machine_creates_its_row(self):
        """Open dispatch -- no machines pre-defined -- the first scan creates
        its own tracking row automatically."""
        from assets.models import AssetPlanningMachine

        planning = self._make_planning(service_type="porte_maquina")
        response = self._submit(planning, "carga", extra={"asset_id": self.machine.id})
        self.assertEqual(response.status_code, 200)
        link = AssetPlanningMachine.objects.get(planning=planning, machine=self.machine)
        self.assertEqual(link.status, "loaded")
        self.assertIsNotNone(link.loaded_at)

    def test_two_predefined_machines_both_load_independently(self):
        from assets.models import AssetPlanningMachine, TransactionTypeChoices

        planning = self._make_planning(service_type="porte_maquina")
        planning.planning_machines.create(machine=self.machine, sequence=1)
        planning.planning_machines.create(machine=self.other_machine, sequence=2, to_zone=self.other_destination)

        r1 = self._submit(planning, "carga", extra={"asset_id": self.machine.id})
        self.assertEqual(r1.status_code, 200)
        r2 = self._submit(planning, "carga", extra={"asset_id": self.other_machine.id})
        self.assertEqual(r2.status_code, 200)

        self.assertEqual(
            AssetPlanningMachine.objects.filter(planning=planning, status="loaded").count(), 2,
        )
        load_txs = planning.transactions.filter(transaction_type=TransactionTypeChoices.PORTE_LOAD)
        self.assertEqual(load_txs.count(), 2)

    def test_carga_rejects_a_machine_not_on_the_plan(self):
        planning = self._make_planning(service_type="porte_maquina")
        planning.planning_machines.create(machine=self.machine, sequence=1)
        response = self._submit(planning, "carga", extra={"asset_id": self.other_machine.id})
        self.assertEqual(response.status_code, 400)
        self.assertIn("coincide", response.json()["error"])

    def test_recarga_of_an_already_loaded_machine_is_idempotent(self):
        planning = self._make_planning(service_type="porte_maquina")
        self._submit(planning, "carga", extra={"asset_id": self.machine.id})
        response = self._submit(planning, "carga", extra={"asset_id": self.machine.id})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json().get("already_done"))

    def test_entrega_requires_asset_id_once_there_are_several_machines(self):
        planning = self._make_planning(service_type="porte_maquina")
        self._submit(planning, "carga", extra={"asset_id": self.machine.id})
        self._submit(planning, "carga", extra={"asset_id": self.other_machine.id})
        response = self._submit(planning, "entrega")
        self.assertEqual(response.status_code, 400)
        self.assertIn("escanear", response.json()["error"].lower())

    def test_entrega_rejects_a_machine_not_on_the_plan(self):
        planning = self._make_planning(service_type="porte_maquina")
        self._submit(planning, "carga", extra={"asset_id": self.machine.id})
        self._submit(planning, "carga", extra={"asset_id": self.other_machine.id})
        intruder = Asset.objects.create(company=self.company, asset_type=self.machine_type, name="MAQUINA-3")
        response = self._submit(planning, "entrega", extra={"asset_id": intruder.id})
        self.assertEqual(response.status_code, 400)
        self.assertIn("pertenece", response.json()["error"])

    def test_planning_stays_in_transit_until_every_machine_is_delivered(self):
        planning = self._make_planning(service_type="porte_maquina")
        self._submit(planning, "carga", extra={"asset_id": self.machine.id})
        self._submit(planning, "carga", extra={"asset_id": self.other_machine.id})

        r1 = self._submit(planning, "entrega", extra={"asset_id": self.machine.id})
        self.assertEqual(r1.status_code, 200)
        planning.refresh_from_db()
        self.assertEqual(planning.status, "in_transit")

        r2 = self._submit(planning, "entrega", extra={"asset_id": self.other_machine.id})
        self.assertEqual(r2.status_code, 200)
        planning.refresh_from_db()
        self.assertEqual(planning.status, "delivered")

    def test_redelivering_an_already_delivered_machine_is_idempotent(self):
        planning = self._make_planning(service_type="porte_maquina")
        self._submit(planning, "carga", extra={"asset_id": self.machine.id})
        self._submit(planning, "carga", extra={"asset_id": self.other_machine.id})
        self._submit(planning, "entrega", extra={"asset_id": self.machine.id})
        response = self._submit(planning, "entrega", extra={"asset_id": self.machine.id})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json().get("already_done"))

    def test_each_machine_is_zone_synced_to_its_own_destination(self):
        """The concrete scenario that drove this feature: two machines on one
        planning, delivered to two different places -- each must end up at
        ITS OWN zone, not the other's or the planning's shared default."""
        planning = self._make_planning(service_type="porte_maquina")
        planning.planning_machines.create(machine=self.machine, sequence=1)  # default: planning.destination
        planning.planning_machines.create(
            machine=self.other_machine, sequence=2, to_zone=self.other_destination,
        )
        self._submit(planning, "carga", extra={"asset_id": self.machine.id})
        self._submit(planning, "carga", extra={"asset_id": self.other_machine.id})
        self._submit(planning, "entrega", extra={"asset_id": self.machine.id})
        self._submit(planning, "entrega", extra={"asset_id": self.other_machine.id})

        self.machine.refresh_from_db()
        self.other_machine.refresh_from_db()
        self.assertEqual(self.machine.current_working_zone_id, self.destination.id)
        self.assertEqual(self.other_machine.current_working_zone_id, self.other_destination.id)

    def test_serializer_exposes_machine_count_and_list(self):
        from api.fleet.serializers import PortePlanningSerializer

        planning = self._make_planning(service_type="porte_maquina")
        planning.planning_machines.create(machine=self.machine, sequence=1)
        planning.planning_machines.create(
            machine=self.other_machine, sequence=2, to_zone=self.other_destination,
        )
        data = PortePlanningSerializer(planning).data
        self.assertEqual(data["machine_count"], 2)
        names = {m["name"] for m in data["machines"]}
        self.assertEqual(names, {"MAQUINA-1", "MAQUINA-2"})
        other_entry = next(m for m in data["machines"] if m["id"] == self.other_machine.id)
        self.assertEqual(other_entry["to_zone_name"], "Otro Destino")

    def test_checklist_has_one_carga_and_one_entrega_step_per_machine(self):
        planning = self._make_planning(service_type="porte_maquina")
        planning.planning_machines.create(machine=self.machine, sequence=1)
        planning.planning_machines.create(machine=self.other_machine, sequence=2)
        checklist = planning.porte_checklist()
        keys = [step["key"] for step in checklist]
        self.assertIn(f"carga_machine_{self.machine.id}", keys)
        self.assertIn(f"carga_machine_{self.other_machine.id}", keys)
        self.assertIn(f"entrega_machine_{self.machine.id}", keys)
        self.assertIn(f"entrega_machine_{self.other_machine.id}", keys)
        # Not the flat single carga/entrega shape once there's more than one.
        self.assertNotIn("carga", keys)
        self.assertNotIn("entrega", keys)

    def test_load_done_is_true_only_once_every_machine_has_started(self):
        planning = self._make_planning(service_type="porte_maquina")
        planning.planning_machines.create(machine=self.machine, sequence=1)
        planning.planning_machines.create(machine=self.other_machine, sequence=2)
        self.assertFalse(planning.porte_progress_flags()["load_done"])
        self._submit(planning, "carga", extra={"asset_id": self.machine.id})
        planning.refresh_from_db()
        self.assertFalse(planning.porte_progress_flags()["load_done"])
        self._submit(planning, "carga", extra={"asset_id": self.other_machine.id})
        planning.refresh_from_db()
        self.assertTrue(planning.porte_progress_flags()["load_done"])
