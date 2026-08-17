"""
assets/tests/test_asset_schedule_view.py

Smoke tests for the Schedule calendar (assets/views/asset_schedule.py):
renders real AssetPlanning data, and move/resize/unassign persist via POST.

Run just this file:
    python manage.py test assets.tests.test_asset_schedule_view
"""
from datetime import date, time, timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext, override

from assets.models import Asset, AssetPlanning, AssetType, AssetTypeCategory, PlanningStatus
from api.fleet.tests.helpers import make_asset_type, make_company, make_employee, make_zone

User = get_user_model()
TODAY = timezone.localdate()


class AssetScheduleViewTests(TestCase):
    def setUp(self):
        self.company = make_company()
        self.origin = make_zone(self.company, name="Origin")
        self.destination = make_zone(self.company, name="Destination")
        self.truck_type = AssetType.objects.create(company=self.company, name="CAMIONES", category=AssetTypeCategory.TRUCK)
        self.truck = Asset.objects.create(company=self.company, asset_type=self.truck_type, name="Truck 1")
        self.cargo = Asset.objects.create(company=self.company, asset_type=make_asset_type(self.company, "Cuba"), name="Cuba 1")

        self.superuser = User.objects.create_superuser(username="admin", password="testpass123", email="a@a.com")
        self.employee = make_employee(self.company, self.superuser)

        self.planning = AssetPlanning.objects.create(
            company=self.company,
            asset=self.cargo,
            origin=self.origin,
            destination=self.destination,
            assigned_to=self.employee,
            transport_asset=self.truck,
            scheduled_date=TODAY,
            scheduled_time=time(8, 0),
            estimated_duration=timedelta(hours=1),
        )

        self.client.force_login(self.superuser)
        self.url = reverse("admin:assets_asset_schedule")

    def test_get_renders_the_planning_as_a_block(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        blocks = response.context["blocks"]
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0]["id"], self.planning.pk)
        self.assertEqual(blocks[0]["start"], 8 * 60)
        self.assertEqual(blocks[0]["end"], 9 * 60)
        self.assertEqual(blocks[0]["label"], self.planning.get_planning_type_display())
        self.assertEqual(blocks[0]["asset"], "Cuba 1")
        self.assertEqual(blocks[0]["origin_name"], "Origin")
        self.assertEqual(blocks[0]["destination_name"], "Destination")
        self.assertNotIn("route", blocks[0])
        self.assertEqual(blocks[0]["truck"], "Truck 1")
        self.assertIn("urgency_label", blocks[0])
        self.assertIn("origin_address", blocks[0])
        self.assertIn("destination_address", blocks[0])
        self.assertIn("load_description", blocks[0])
        self.assertIn("notes", blocks[0])
        self.assertIn("color", blocks[0])
        self.assertEqual(blocks[0]["status_color"], self.planning.status_color)
        self.assertNotIn("project_file", blocks[0])
        self.assertNotIn("expediente", blocks[0])

    def test_block_includes_planning_details(self):
        self.planning.load_description = "Escombros mixtos"
        self.planning.notes = "Llamar al encargado"
        self.planning.cuba_size = "7m3"
        self.planning.cuba_operation_type = "puesta"
        self.planning.urgency = "urgent"
        self.planning.service_type = "movimiento_cuba"
        self.origin.street_address = "Calle Origen 1"
        self.origin.city = "Málaga"
        self.origin.save()
        self.destination.street_address = "Calle Destino 2"
        self.destination.city = "Málaga"
        self.destination.save()
        self.planning.save()

        response = self.client.get(self.url)
        block = response.context["blocks"][0]
        self.assertEqual(block["load_description"], "Escombros mixtos")
        self.assertEqual(block["notes"], "Llamar al encargado")
        self.assertEqual(block["cuba_size"], "7m3")
        self.assertEqual(block["cuba_size_label"], "7 M3")
        self.assertEqual(block["cuba_operation"], "puesta")
        self.assertEqual(block["urgency"], "urgent")
        self.assertEqual(block["color"], "#F59E0B")  # movimiento_cuba
        self.assertEqual(block["urgency_color"], "#EF4444")
        self.assertIn("Calle Origen 1", block["origin_address"])
        self.assertIn("Calle Destino 2", block["destination_address"])
        self.assertEqual(block["service_type"], "movimiento_cuba")

    def test_backlog_item_includes_planning_details(self):
        planning = AssetPlanning.objects.create(
            company=self.company, asset=self.cargo, origin=self.origin, destination=self.destination,
            scheduled_date=TODAY, load_description="Arena", notes="Urgente al mediodía",
            cuba_size="3m3", urgency="high", service_type="porte_maquina",
            estimated_duration=timedelta(hours=1, minutes=30),
        )
        response = self.client.get(self.url)
        item = next(i for i in response.context["backlog_items"] if i["id"] == planning.pk)
        self.assertEqual(item["load_description"], "Arena")
        self.assertEqual(item["notes"], "Urgente al mediodía")
        self.assertEqual(item["cuba_size_label"], "3 M3")
        self.assertEqual(item["origin_name"], "Origin")
        self.assertEqual(item["destination_name"], "Destination")
        self.assertNotIn("route", item)
        self.assertEqual(item["urgency"], "high")
        self.assertEqual(item["color"], "#3B82F6")  # porte_maquina
        self.assertEqual(item["urgency_color"], "#F59E0B")
        self.assertEqual(item["minutes"], 90)
        self.assertEqual(item["duration_label"], "1h 30 min")

    def test_resize_persists_new_times(self):
        response = self.client.post(
            self.url,
            data={"action": "resize", "id": self.planning.pk, "start": 8 * 60, "end": 10 * 60},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.planning.refresh_from_db()
        self.assertEqual(self.planning.scheduled_time, time(8, 0))
        self.assertEqual(self.planning.scheduled_end_time, time(10, 0))

    def test_move_persists_new_person_and_day(self):
        other_user = User.objects.create_user(username="other", password="testpass123")
        other_employee = make_employee(self.company, other_user)
        new_day = TODAY + timedelta(days=1)

        response = self.client.post(
            self.url,
            data={
                "action": "move", "id": self.planning.pk,
                "start": 9 * 60, "end": 10 * 60,
                "person_id": other_employee.pk, "day": new_day.isoformat(),
            },
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.planning.refresh_from_db()
        self.assertEqual(self.planning.assigned_to_id, other_employee.pk)
        self.assertEqual(self.planning.scheduled_date, new_day)
        self.assertEqual(self.planning.scheduled_time, time(9, 0))

    def test_move_reactivates_a_not_completed_planning(self):
        self.planning.status = PlanningStatus.NOT_COMPLETED
        self.planning.abandoned_stage = "picked_up_pending_pour"
        self.planning.abandoned_at = timezone.now()
        self.planning.save(update_fields=["status", "abandoned_stage", "abandoned_at"])
        new_day = TODAY + timedelta(days=1)

        response = self.client.post(
            self.url,
            data={
                "action": "move", "id": self.planning.pk,
                "start": 9 * 60, "end": 10 * 60,
                "person_id": self.employee.pk, "day": new_day.isoformat(),
            },
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.planning.refresh_from_db()
        self.assertEqual(self.planning.status, PlanningStatus.PENDING)
        self.assertEqual(self.planning.abandoned_stage, "")
        self.assertIsNone(self.planning.abandoned_at)

    def test_unassign_clears_driver_truck_and_time(self):
        response = self.client.post(
            self.url,
            data={"action": "unassign", "id": self.planning.pk},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.planning.refresh_from_db()
        self.assertIsNone(self.planning.assigned_to)
        self.assertIsNone(self.planning.transport_asset)
        self.assertIsNone(self.planning.scheduled_time)
        self.assertIsNone(self.planning.scheduled_end_time)

    def test_resize_rejects_end_before_start(self):
        response = self.client.post(
            self.url,
            data={"action": "resize", "id": self.planning.pk, "start": 10 * 60, "end": 9 * 60},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)

    def test_duplicate_next_day_creates_a_clone_dated_one_day_later(self):
        self.planning.suggested_trucks.set([self.truck])
        response = self.client.post(
            self.url,
            data={"action": "duplicate_next_day", "id": self.planning.pk},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data["success"])

        clones = AssetPlanning.objects.exclude(pk=self.planning.pk)
        self.assertEqual(clones.count(), 1)
        clone = clones.get()
        self.assertEqual(clone.scheduled_date, TODAY + timedelta(days=1))
        self.assertEqual(clone.asset_id, self.planning.asset_id)
        self.assertEqual(clone.assigned_to_id, self.employee.pk)
        self.assertEqual(clone.transport_asset_id, self.truck.pk)
        self.assertEqual(clone.scheduled_time, time(8, 0))
        self.assertEqual(clone.scheduled_end_time, time(9, 0))
        self.assertEqual(list(clone.suggested_trucks.all()), [self.truck])

        block = data["block"]
        self.assertEqual(block["id"], clone.pk)
        self.assertEqual(block["date"], (TODAY + timedelta(days=1)).isoformat())
        self.assertEqual(block["assigned_to_id"], self.employee.pk)
        self.assertEqual(block["start"], 8 * 60)
        self.assertEqual(block["end"], 9 * 60)

        # Original untouched.
        self.planning.refresh_from_db()
        self.assertEqual(self.planning.scheduled_date, TODAY)

    def test_context_exposes_add_and_change_urls(self):
        response = self.client.get(self.url)
        self.assertEqual(response.context["add_url"], reverse("admin:assets_assetplanning_add"))
        self.assertIn("/0/change/", response.context["change_url_template"])

    def test_backlog_shows_every_unassigned_planning_no_cap(self):
        extra = [
            AssetPlanning.objects.create(
                company=self.company, asset=self.cargo, origin=self.origin, destination=self.destination,
                scheduled_date=TODAY,
            )
            for _ in range(5)
        ]
        response = self.client.get(self.url)
        backlog_ids = {item["id"] for item in response.context["backlog_items"]}
        self.assertEqual(backlog_ids, {p.pk for p in extra})

    def test_backlog_item_reports_all_missing_fields(self):
        planning = AssetPlanning.objects.create(
            company=self.company, asset=self.cargo, origin=self.origin, destination=self.destination,
            scheduled_date=TODAY,
        )
        with override("es"):
            response = self.client.get(self.url)
            item = next(i for i in response.context["backlog_items"] if i["id"] == planning.pk)
            self.assertCountEqual(item["missing"], [gettext("driver"), gettext("truck"), gettext("time")])
            self.assertEqual(item["time_label"], "")

    def test_backlog_item_shows_time_label_when_scheduled_time_is_set(self):
        # Missing only driver/truck — still in the backlog, but the time it
        # already has should be visible on the card, not just "missing: []".
        planning = AssetPlanning.objects.create(
            company=self.company, asset=self.cargo, origin=self.origin, destination=self.destination,
            scheduled_date=TODAY, scheduled_time=time(9, 30),
        )
        response = self.client.get(self.url)
        item = next(i for i in response.context["backlog_items"] if i["id"] == planning.pk)
        self.assertEqual(item["time_label"], "09:30")
        self.assertNotIn(gettext("time"), item["missing"])

    def test_move_on_backlog_item_assigns_driver_date_and_time(self):
        planning = AssetPlanning.objects.create(
            company=self.company, asset=self.cargo, origin=self.origin, destination=self.destination,
            scheduled_date=TODAY,
        )
        response = self.client.post(
            self.url,
            data={
                "action": "move", "id": planning.pk,
                "start": 10 * 60, "end": 11 * 60,
                "person_id": self.employee.pk, "day": TODAY.isoformat(),
            },
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        planning.refresh_from_db()
        self.assertEqual(planning.assigned_to_id, self.employee.pk)
        self.assertEqual(planning.scheduled_time, time(10, 0))
        self.assertEqual(planning.scheduled_end_time, time(11, 0))
        # transport_asset is still missing, so it's tagged "unassigned" (not
        # "manual") until a truck is picked too — see AssetPlanning.save().
        self.assertEqual(planning.assignment_origin, "unassigned")
        self.assertEqual(response.json()["truck"], "")
        self.assertTrue(response.json()["truck_note"])

    def test_move_auto_links_the_driver_s_single_assigned_truck(self):
        self.truck.assigned_to = self.employee
        self.truck.save(update_fields=["assigned_to"])
        planning = AssetPlanning.objects.create(
            company=self.company, asset=self.cargo, origin=self.origin, destination=self.destination,
            scheduled_date=TODAY,
        )
        response = self.client.post(
            self.url,
            data={
                "action": "move", "id": planning.pk,
                "start": 10 * 60, "end": 11 * 60,
                "person_id": self.employee.pk, "day": TODAY.isoformat(),
            },
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["truck"], "Truck 1")
        self.assertIsNone(data["truck_note"])
        planning.refresh_from_db()
        self.assertEqual(planning.transport_asset_id, self.truck.pk)

    def test_move_lists_multiple_driver_trucks_as_suggestions_without_picking_one(self):
        truck2 = Asset.objects.create(company=self.company, asset_type=self.truck_type, name="Truck 2")
        self.truck.assigned_to = self.employee
        self.truck.save(update_fields=["assigned_to"])
        truck2.assigned_to = self.employee
        truck2.save(update_fields=["assigned_to"])
        planning = AssetPlanning.objects.create(
            company=self.company, asset=self.cargo, origin=self.origin, destination=self.destination,
            scheduled_date=TODAY,
        )
        response = self.client.post(
            self.url,
            data={
                "action": "move", "id": planning.pk,
                "start": 10 * 60, "end": 11 * 60,
                "person_id": self.employee.pk, "day": TODAY.isoformat(),
            },
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["truck"], "")
        self.assertTrue(data["truck_note"])
        planning.refresh_from_db()
        self.assertIsNone(planning.transport_asset)
        self.assertCountEqual(planning.suggested_trucks.all(), [self.truck, truck2])

    def test_move_adds_to_suggested_trucks_instead_of_replacing_them(self):
        # suggested_trucks is a hint for whoever assigns this later (by hand
        # or by a future auto-assigner) — it should only ever grow here, never
        # get wiped out by a driver drag, so a curated/previous list survives.
        curated_truck = Asset.objects.create(company=self.company, asset_type=self.truck_type, name="Curated")
        truck2 = Asset.objects.create(company=self.company, asset_type=self.truck_type, name="Truck 2")
        self.truck.assigned_to = self.employee
        self.truck.save(update_fields=["assigned_to"])
        truck2.assigned_to = self.employee
        truck2.save(update_fields=["assigned_to"])
        planning = AssetPlanning.objects.create(
            company=self.company, asset=self.cargo, origin=self.origin, destination=self.destination,
            scheduled_date=TODAY,
        )
        planning.suggested_trucks.add(curated_truck)

        response = self.client.post(
            self.url,
            data={
                "action": "move", "id": planning.pk,
                "start": 10 * 60, "end": 11 * 60,
                "person_id": self.employee.pk, "day": TODAY.isoformat(),
            },
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        planning.refresh_from_db()
        self.assertCountEqual(planning.suggested_trucks.all(), [curated_truck, self.truck, truck2])

    def test_move_never_overrides_an_already_assigned_truck(self):
        other_user = User.objects.create_user(username="other3", password="testpass123")
        other_employee = make_employee(self.company, other_user)
        other_truck = Asset.objects.create(company=self.company, asset_type=self.truck_type, name="Truck 2")
        other_truck.assigned_to = other_employee
        other_truck.save(update_fields=["assigned_to"])
        # self.planning already has transport_asset=self.truck from setUp.
        response = self.client.post(
            self.url,
            data={
                "action": "move", "id": self.planning.pk,
                "start": 9 * 60, "end": 10 * 60,
                "person_id": other_employee.pk, "day": TODAY.isoformat(),
            },
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.planning.refresh_from_db()
        self.assertEqual(self.planning.transport_asset_id, self.truck.pk)

    def test_move_swaps_the_truck_when_it_was_only_carried_over_from_the_previous_driver(self):
        # self.employee owns self.truck; moving self.planning (auto-linked to
        # self.truck via self.employee) to a driver with their own truck should
        # swap it, not leave self.truck stuck on the job forever.
        self.truck.assigned_to = self.employee
        self.truck.save(update_fields=["assigned_to"])
        other_user = User.objects.create_user(username="other4", password="testpass123")
        other_employee = make_employee(self.company, other_user)
        other_truck = Asset.objects.create(company=self.company, asset_type=self.truck_type, name="Truck 2")
        other_truck.assigned_to = other_employee
        other_truck.save(update_fields=["assigned_to"])
        # self.planning already has transport_asset=self.truck from setUp — as
        # if it had been auto-linked to self.employee's own truck earlier.

        response = self.client.post(
            self.url,
            data={
                "action": "move", "id": self.planning.pk,
                "start": 9 * 60, "end": 10 * 60,
                "person_id": other_employee.pk, "day": TODAY.isoformat(),
            },
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["truck"], "Truck 2")
        self.assertIsNone(data["truck_note"])
        self.planning.refresh_from_db()
        self.assertEqual(self.planning.transport_asset_id, other_truck.pk)

    def test_move_carried_over_swap_never_clears_existing_suggested_trucks(self):
        curated_truck = Asset.objects.create(company=self.company, asset_type=self.truck_type, name="Curated")
        self.planning.suggested_trucks.add(curated_truck)
        self.truck.assigned_to = self.employee
        self.truck.save(update_fields=["assigned_to"])
        other_user = User.objects.create_user(username="other5", password="testpass123")
        other_employee = make_employee(self.company, other_user)
        other_truck = Asset.objects.create(company=self.company, asset_type=self.truck_type, name="Truck 2")
        other_truck.assigned_to = other_employee
        other_truck.save(update_fields=["assigned_to"])

        response = self.client.post(
            self.url,
            data={
                "action": "move", "id": self.planning.pk,
                "start": 9 * 60, "end": 10 * 60,
                "person_id": other_employee.pk, "day": TODAY.isoformat(),
            },
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.planning.refresh_from_db()
        self.assertEqual(self.planning.transport_asset_id, other_truck.pk)
        self.assertIn(curated_truck, self.planning.suggested_trucks.all())

    @patch("assets.services.transport_assigner.GoogleRouteOptimizer.estimate_duration", return_value=timedelta(hours=1))
    def test_auto_assign_action_resolves_a_backlog_item(self, _mock):
        self.truck.assigned_to = self.employee
        self.truck.save(update_fields=["assigned_to"])
        planning = AssetPlanning.objects.create(
            company=self.company, asset=self.cargo, origin=self.origin, destination=self.destination,
            scheduled_date=TODAY,
        )
        response = self.client.post(
            self.url,
            data={"action": "auto_assign", "id": planning.pk},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data["success"])
        self.assertEqual(data["block"]["id"], planning.pk)
        self.assertEqual(data["block"]["assigned_to_id"], self.employee.pk)
        self.assertEqual(data["block"]["date"], TODAY.isoformat())
        planning.refresh_from_db()
        self.assertEqual(planning.transport_asset_id, self.truck.pk)
        self.assertEqual(planning.assigned_to_id, self.employee.pk)
        self.assertIsNotNone(planning.scheduled_time)

    def test_auto_assign_action_returns_an_error_when_nothing_is_available(self):
        # self.truck has no assigned_to and no MachinePermission holder.
        planning = AssetPlanning.objects.create(
            company=self.company, asset=self.cargo, origin=self.origin, destination=self.destination,
            scheduled_date=TODAY,
        )
        response = self.client.post(
            self.url,
            data={"action": "auto_assign", "id": planning.pk},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)
        planning.refresh_from_db()
        self.assertIsNone(planning.transport_asset)

    def test_schedule_context_exposes_capture_url(self):
        response = self.client.get(self.url)
        self.assertEqual(
            response.context["capture_url"],
            reverse("admin:assets_asset_schedule_capture"),
        )

    def test_filter_by_service_type(self):
        self.planning.service_type = "movimiento_cuba"
        self.planning.save(update_fields=["service_type"])
        other = AssetPlanning.objects.create(
            company=self.company,
            asset=self.cargo,
            origin=self.origin,
            destination=self.destination,
            assigned_to=self.employee,
            transport_asset=self.truck,
            scheduled_date=TODAY,
            scheduled_time=time(10, 0),
            estimated_duration=timedelta(hours=1),
            service_type="porte_maquina",
        )
        response = self.client.get(self.url, {"service_type": "movimiento_cuba"})
        block_ids = {b["id"] for b in response.context["blocks"]}
        self.assertIn(self.planning.pk, block_ids)
        self.assertNotIn(other.pk, block_ids)

    def test_filter_by_driver_limits_rows(self):
        other_user = User.objects.create_user(username="other", password="testpass123")
        other = make_employee(self.company, other_user)
        response = self.client.get(self.url, {"assigned_to": other.pk})
        people_ids = {p["pk"] for p in response.context["people"]}
        self.assertEqual(people_ids, {other.pk})
        self.assertEqual(response.context["blocks"], [])

    def test_filter_only_with_plans_hides_empty_drivers(self):
        other_user = User.objects.create_user(username="other2", password="testpass123")
        make_employee(self.company, other_user)
        response = self.client.get(self.url, {"only_with_plans": "on"})
        people_ids = {p["pk"] for p in response.context["people"]}
        self.assertEqual(people_ids, {self.employee.pk})

    def test_filter_by_urgency(self):
        self.planning.urgency = "urgent"
        self.planning.save(update_fields=["urgency"])
        response = self.client.get(self.url, {"urgency": "low"})
        self.assertEqual(response.context["blocks"], [])
        response = self.client.get(self.url, {"urgency": "urgent"})
        self.assertEqual(len(response.context["blocks"]), 1)


class AssetScheduleCaptureViewTests(TestCase):
    def setUp(self):
        self.company = make_company()
        self.origin = make_zone(self.company, name="Origin")
        self.origin.street_address = "Calle A 1"
        self.origin.city = "Málaga"
        self.origin.save()
        self.destination = make_zone(self.company, name="Destination")
        self.destination.street_address = "Calle B 2"
        self.destination.city = "Málaga"
        self.destination.save()
        self.truck_type = AssetType.objects.create(
            company=self.company, name="CAMIONES", category=AssetTypeCategory.TRUCK
        )
        self.truck = Asset.objects.create(
            company=self.company, asset_type=self.truck_type, name="Truck 1"
        )
        self.cargo = Asset.objects.create(
            company=self.company, asset_type=make_asset_type(self.company, "Cuba"), name="Cuba 1"
        )
        self.superuser = User.objects.create_superuser(
            username="admin", password="testpass123", email="a@a.com"
        )
        self.employee = make_employee(self.company, self.superuser)
        self.planning = AssetPlanning.objects.create(
            company=self.company,
            asset=self.cargo,
            origin=self.origin,
            destination=self.destination,
            assigned_to=self.employee,
            transport_asset=self.truck,
            scheduled_date=TODAY,
            scheduled_time=time(8, 0),
            estimated_duration=timedelta(hours=1),
            load_description="Escombros",
            notes="Llamar al encargado",
            cuba_size="7m3",
            urgency="high",
        )
        self.client.force_login(self.superuser)
        self.url = reverse("admin:assets_asset_schedule_capture")

    def test_capture_groups_jobs_by_driver_with_full_detail(self):
        response = self.client.get(self.url, {"day": TODAY.isoformat()})
        self.assertEqual(response.status_code, 200)
        groups = response.context["groups"]
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["person_id"], self.employee.pk)
        self.assertEqual(groups[0]["job_count"], 1)
        job = groups[0]["jobs"][0]
        self.assertEqual(job["label"], self.planning.get_planning_type_display())
        self.assertEqual(job["asset"], "Cuba 1")
        self.assertEqual(job["time_label"], "08:00–09:00")
        self.assertEqual(job["load_description"], "Escombros")
        self.assertEqual(job["notes"], "Llamar al encargado")
        self.assertEqual(job["cuba_size_label"], "7 M3")
        self.assertIn("Calle A 1", job["origin_address"])
        self.assertIn("Calle B 2", job["destination_address"])
        self.assertEqual(job["truck"], "Truck 1")
        self.assertEqual(response.context["total_jobs"], 1)

    def test_capture_filters_by_person(self):
        other_user = User.objects.create_user(username="other", password="testpass123")
        other = make_employee(self.company, other_user)
        response = self.client.get(
            self.url,
            {"day": TODAY.isoformat(), "person": other.pk},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["selected_person_id"], other.pk)
        self.assertEqual(response.context["groups"][0]["person_id"], other.pk)
        self.assertEqual(response.context["groups"][0]["job_count"], 0)
        self.assertEqual(response.context["total_jobs"], 0)

    def test_capture_ignores_other_days(self):
        other_day = TODAY + timedelta(days=1)
        self.planning.scheduled_date = other_day
        self.planning.save(update_fields=["scheduled_date"])
        response = self.client.get(self.url, {"day": TODAY.isoformat()})
        self.assertEqual(response.context["total_jobs"], 0)
