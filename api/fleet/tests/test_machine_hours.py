"""
api/fleet/tests/test_machine_hours.py
"""
import json
from datetime import date, timedelta
from django.utils import timezone
from rest_framework.test import APITestCase, APIClient
from assets.models import (
    AssetPlanning,
    AssetTransaction,
    TransactionTypeChoices,
)
from api.fleet.tests.helpers import (
    ApiFixtureMixin,
    make_almacen,
    make_asset,
    make_asset_type,
    make_company,
    make_employee,
    make_obra,
    make_user,
)

# ─── Shared helpers ───────────────────────────────────────────────────────────

def make_machine_planning(asset, employee, company, status="pending", scheduled_date=None):
    return AssetPlanning.objects.create(
        asset=asset,
        assigned_to=employee,
        company=company,
        scheduled_date=scheduled_date or date.today(),
        status=status,
        planning_type="machine_hours",
    )


def make_open_tx(asset, employee, company, planning_id, zone=None):
    now = timezone.now()
    return AssetTransaction.objects.create(
        asset=asset,
        company=company,
        performed_by=employee,
        transaction_type=TransactionTypeChoices.MACHINE_HOURS,
        from_zone=zone,
        date=now,
        notes="Test start",
        data={
            "planning_id": planning_id,
            "machine_start_time": timezone.localtime(now).isoformat(),
            "intervals": [{"start": "09:00:00", "end": None}],
        },
    )


def make_completed_tx(asset, employee, company, planning_id, zone=None, days_ago=0):
    start = timezone.localtime() - timedelta(days=days_ago)
    end = start + timedelta(hours=2)
    return AssetTransaction.objects.create(
        asset=asset,
        company=company,
        performed_by=employee,
        transaction_type=TransactionTypeChoices.MACHINE_HOURS,
        from_zone=zone,
        date=start,
        notes="Test completed",
        data={
            "planning_id": planning_id,
            "machine_start_time": start.isoformat(),
            "machine_end_time": end.isoformat(),
            "intervals": [{"start": "09:00:00", "end": "11:00:00"}],
        },
    )


# ─── Base test case ───────────────────────────────────────────────────────────

class MachineHoursBaseTest(ApiFixtureMixin, APITestCase):
    def setUp(self):
        super().setUp()
        self.asset_type = make_asset_type(self.company, name="MAQUINAS")
        self.zone = make_obra(self.company, name="Obra Test", lat=40.406, lon=-3.683)
        self.asset = make_asset(
            self.company, self.asset_type,
            employee=self.employee, zone=self.zone, name="Maquina 1"
        )


# ─── MachineHoursListView ─────────────────────────────────────────────────────

class MachineHoursListViewTest(MachineHoursBaseTest):
    def test_returns_todays_pending_plannings(self):
        planning = make_machine_planning(self.asset, self.employee, self.company)
        response = self.client.get("/en/api/machine-hours/")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(len(data["machines"]), 1)
        self.assertEqual(data["machines"][0]["planning_id"], planning.id)
        self.assertEqual(data["machines"][0]["estado"], "none")

    def test_excludes_delivered_plannings(self):
        make_machine_planning(self.asset, self.employee, self.company, status="delivered")
        response = self.client.get("/en/api/machine-hours/")
        self.assertEqual(len(response.json()["machines"]), 0)

    def test_excludes_other_days(self):
        make_machine_planning(
            self.asset, self.employee, self.company,
            scheduled_date=date.today() - timedelta(days=1)
        )
        response = self.client.get("/en/api/machine-hours/")
        self.assertEqual(len(response.json()["machines"]), 0)

    def test_estado_working(self):
        planning = make_machine_planning(self.asset, self.employee, self.company)
        make_open_tx(self.asset, self.employee, self.company, planning.id, self.zone)
        response = self.client.get("/en/api/machine-hours/")
        self.assertEqual(response.json()["machines"][0]["estado"], "working")

    def test_estado_paused(self):
        planning = make_machine_planning(self.asset, self.employee, self.company)
        tx = make_open_tx(self.asset, self.employee, self.company, planning.id, self.zone)
        tx.data["intervals"][0]["end"] = "10:00:00"
        tx.save()
        response = self.client.get("/en/api/machine-hours/")
        self.assertEqual(response.json()["machines"][0]["estado"], "paused")

    def test_estado_completed(self):
        planning = make_machine_planning(self.asset, self.employee, self.company)
        make_completed_tx(self.asset, self.employee, self.company, planning.id, self.zone)
        response = self.client.get("/en/api/machine-hours/")
        self.assertEqual(response.json()["machines"][0]["estado"], "completed")

    def test_does_not_mix_plannings(self):
        planning1 = make_machine_planning(self.asset, self.employee, self.company)
        planning2 = make_machine_planning(self.asset, self.employee, self.company)
        make_open_tx(self.asset, self.employee, self.company, planning1.id, self.zone)
        response = self.client.get("/en/api/machine-hours/")
        data = response.json()
        self.assertEqual(len(data["machines"]), 2)
        estados = {m["planning_id"]: m["estado"] for m in data["machines"]}
        self.assertEqual(estados[planning1.id], "working")
        self.assertEqual(estados[planning2.id], "none")

    def test_excludes_other_employees(self):
        other_user = make_user(username="other")
        other_employee = make_employee(self.company, other_user)
        make_machine_planning(self.asset, other_employee, self.company)
        response = self.client.get("/en/api/machine-hours/")
        self.assertEqual(len(response.json()["machines"]), 0)


# ─── MachineHoursStrategy ─────────────────────────────────────────────────────

class MachineHoursStrategyTest(MachineHoursBaseTest):
    def setUp(self):
        super().setUp()
        self.planning = make_machine_planning(self.asset, self.employee, self.company)
        self.url = f"/en/api/assets/{self.asset.id}/transaction/"

    def _post(self, action, extra=None):
        payload = {
            "transaction_type": "machine_hours",
            "action": action,
            "planning_id": self.planning.id,
            "latitude": 40.406,
            "longitude": -3.683,
        }
        if extra:
            payload.update(extra)
        return self.client.post(
            self.url,
            data=json.dumps(payload),
            content_type="application/json"
        )

    def _get_tx(self):
        return self._get_tx_by_planning_id(self.planning.id)

    def _get_tx_by_planning_id(self, planning_id):
        return AssetTransaction.objects.filter(
            transaction_type=TransactionTypeChoices.MACHINE_HOURS,
            data__planning_id=planning_id,
        ).first()

    def test_start_creates_transaction(self):
        response = self._post("start")
        self.assertEqual(response.status_code, 200)
        tx = self._get_tx()
        self.assertIsNotNone(tx)
        self.assertIsNotNone(tx.data.get("machine_start_time"))
        self.assertNotIn("machine_end_time", tx.data)
        self.assertEqual(len(tx.data["intervals"]), 1)
        self.assertIsNone(tx.data["intervals"][0]["end"])
        # The new FK is populated alongside the existing soft data["planning_id"].
        self.assertEqual(tx.planning_id, self.planning.id)

    def test_start_uses_the_planning_project_file_over_the_zone(self):
        from management.models import ProjectFile
        from sales.models import Client

        client = Client.objects.create(name="Client 1", company=self.company)
        zone_file = ProjectFile.objects.create(name="Zone", client=client, company=self.company)
        plan_file = ProjectFile.objects.create(name="Plan", client=client, company=self.company)
        self.zone.project_file = zone_file
        self.zone.save(update_fields=["project_file"])
        self.planning.project_file = plan_file
        self.planning.save(update_fields=["project_file"])

        response = self._post("start")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self._get_tx().project_file_id, plan_file.pk)

    def test_start_falls_back_to_the_zone_when_the_plan_has_no_project_file(self):
        from management.models import ProjectFile
        from sales.models import Client

        client = Client.objects.create(name="Client 1", company=self.company)
        zone_file = ProjectFile.objects.create(name="Zone", client=client, company=self.company)
        self.zone.project_file = zone_file
        self.zone.save(update_fields=["project_file"])

        response = self._post("start")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self._get_tx().project_file_id, zone_file.pk)

    def test_start_with_unknown_planning_id_leaves_fk_blank(self):
        response = self._post("start", extra={"planning_id": 999999})
        self.assertEqual(response.status_code, 200)
        tx = self._get_tx_by_planning_id(999999)
        self.assertIsNotNone(tx)
        self.assertIsNone(tx.planning_id)
        self.assertEqual(tx.data["planning_id"], 999999)

    def test_start_double_start_rejected(self):
        self._post("start")
        response = self._post("start")
        self.assertEqual(response.status_code, 400)

    def test_pause_closes_interval(self):
        self._post("start")
        response = self._post("pause")
        self.assertEqual(response.status_code, 200)
        tx = self._get_tx()
        self.assertIsNotNone(tx.data["intervals"][0]["end"])

    def test_pause_when_already_paused_rejected(self):
        self._post("start")
        self._post("pause")
        response = self._post("pause")
        self.assertEqual(response.status_code, 400)

    def test_resume_opens_new_interval(self):
        self._post("start")
        self._post("pause")
        response = self._post("resume")
        self.assertEqual(response.status_code, 200)
        tx = self._get_tx()
        self.assertEqual(len(tx.data["intervals"]), 2)
        self.assertIsNone(tx.data["intervals"][1]["end"])

    def test_resume_when_working_rejected(self):
        self._post("start")
        response = self._post("resume")
        self.assertEqual(response.status_code, 400)

    def test_end_sets_machine_end_time(self):
        self._post("start")
        response = self._post("end")
        self.assertEqual(response.status_code, 200)
        tx = self._get_tx()
        self.assertIsNotNone(tx.data.get("machine_end_time"))
        self.assertIsNotNone(tx.data["intervals"][0]["end"])

    def test_end_updates_planning_to_delivered(self):
        self._post("start")
        self._post("end")
        self.planning.refresh_from_db()
        self.assertEqual(self.planning.status, "delivered")

    def test_end_when_no_open_tx_rejected(self):
        response = self._post("end")
        self.assertEqual(response.status_code, 404)

    def test_full_flow_start_pause_resume_end(self):
        self._post("start")
        self._post("pause")
        self._post("resume")
        response = self._post("end")
        self.assertEqual(response.status_code, 200)
        tx = self._get_tx()
        self.assertEqual(len(tx.data["intervals"]), 2)
        self.assertIsNotNone(tx.data["intervals"][0]["end"])
        self.assertIsNotNone(tx.data["intervals"][1]["end"])
        self.assertIsNotNone(tx.data.get("machine_end_time"))

    def test_invalid_action_rejected(self):
        response = self._post("invalid")
        self.assertEqual(response.status_code, 400)


# ─── MovementsListView excludes machine_hours ─────────────────────────────────

class MovementsExcludesMachineHoursTest(MachineHoursBaseTest):
    def test_machine_hours_excluded_from_movements(self):
        make_completed_tx(self.asset, self.employee, self.company, planning_id=1)
        AssetTransaction.objects.create(
            asset=self.asset,
            company=self.company,
            performed_by=self.employee,
            transaction_type=TransactionTypeChoices.MOVEMENT,
            date=timezone.now(),
            data={},
        )
        response = self.client.get("/en/api/movements/")
        self.assertEqual(response.status_code, 200)
        types = [r["transaction_type"] for r in response.json()["results"]]
        self.assertNotIn("machine_hours", types)
        self.assertIn("movement", types)


# ─── MachineHoursHistorialView ────────────────────────────────────────────────

class MachineHoursHistorialViewTest(MachineHoursBaseTest):
    def test_returns_completed_transactions(self):
        make_completed_tx(self.asset, self.employee, self.company, planning_id=1)
        response = self.client.get("/en/api/machine-hours/history/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["count"], 1)

    def test_excludes_open_transactions(self):
        make_open_tx(self.asset, self.employee, self.company, planning_id=1, zone=self.zone)
        response = self.client.get("/en/api/machine-hours/history/")
        self.assertEqual(response.json()["count"], 0)

    def test_date_from_filter(self):
        make_completed_tx(self.asset, self.employee, self.company, planning_id=1, days_ago=5)
        make_completed_tx(self.asset, self.employee, self.company, planning_id=2, days_ago=1)
        date_from = (date.today() - timedelta(days=2)).strftime("%Y-%m-%d")
        response = self.client.get(f"/en/api/machine-hours/history/?date_from={date_from}")
        self.assertEqual(response.json()["count"], 1)

    def test_date_to_filter(self):
        make_completed_tx(self.asset, self.employee, self.company, planning_id=1, days_ago=5)
        make_completed_tx(self.asset, self.employee, self.company, planning_id=2, days_ago=1)
        date_to = (date.today() - timedelta(days=3)).strftime("%Y-%m-%d")
        response = self.client.get(f"/en/api/machine-hours/history/?date_to={date_to}")
        self.assertEqual(response.json()["count"], 1)

    def test_pagination(self):
        for i in range(25):
            make_completed_tx(self.asset, self.employee, self.company, planning_id=i)
        response = self.client.get("/en/api/machine-hours/history/")
        data = response.json()
        self.assertEqual(len(data["results"]), 20)
        self.assertIsNotNone(data["next"])

    def test_excludes_other_employees(self):
        other_user = make_user(username="other")
        other_employee = make_employee(self.company, other_user)
        make_completed_tx(self.asset, other_employee, self.company, planning_id=99)
        response = self.client.get("/en/api/machine-hours/history/")
        self.assertEqual(response.json()["count"], 0)


class MachineCheckViewTest(MachineHoursBaseTest):
    def test_any_employee_in_the_company_can_check_the_machine(self):
        other_user = make_user(username="other_worker")
        other_employee = make_employee(self.company, other_user)
        other_client = APIClient()
        other_client.force_authenticate(user=other_user)
        response = other_client.get(f"/en/api/assets/{self.asset.id}/machine-check/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["asset_id"], self.asset.id)

    def test_a_machine_from_another_company_is_not_found(self):
        other_company = make_company(name="Other Co")
        other_asset_type = make_asset_type(other_company, name="MAQUINAS")
        other_asset = make_asset(other_company, other_asset_type, name="Maquina Ajena")
        response = self.client.get(f"/en/api/assets/{other_asset.id}/machine-check/")
        self.assertEqual(response.status_code, 404)


class MachineHoursZoneFollowsWorkerTest(MachineHoursBaseTest):
    def setUp(self):
        super().setUp()
        self.planning = make_machine_planning(self.asset, self.employee, self.company)
        self.url = f"/en/api/assets/{self.asset.id}/transaction/"
        self.almacen = make_almacen(self.company, name="Almacen Test", lat=41.0, lon=-4.0)

    def _post(self, action, extra=None):
        payload = {
            "transaction_type": "machine_hours",
            "action": action,
            "planning_id": self.planning.id,
        }
        if extra:
            payload.update(extra)
        return self.client.post(self.url, data=json.dumps(payload), content_type="application/json")

    def test_start_moves_the_machine_to_a_zone_sent_by_id(self):
        response = self._post("start", extra={"zone_id": self.almacen.id})
        self.assertEqual(response.status_code, 200)
        self.asset.refresh_from_db()
        self.assertEqual(self.asset.current_working_zone_id, self.almacen.id)

    def test_start_moves_the_machine_to_a_gps_matched_almacen_not_just_obras(self):
        lat, lon = self.almacen.get_coordinates()
        response = self._post("start", extra={"latitude": lat, "longitude": lon})
        self.assertEqual(response.status_code, 200)
        self.asset.refresh_from_db()
        self.assertEqual(self.asset.current_working_zone_id, self.almacen.id)

    def test_start_with_no_gps_and_no_zone_id_leaves_the_zone_unchanged(self):
        response = self._post("start")
        self.assertEqual(response.status_code, 200)
        self.asset.refresh_from_db()
        self.assertEqual(self.asset.current_working_zone_id, self.zone.id)

    def test_start_far_from_any_zone_no_longer_errors(self):
        response = self._post("start", extra={"latitude": 0.0, "longitude": 0.0})
        self.assertEqual(response.status_code, 200)
        self.asset.refresh_from_db()
        self.assertEqual(self.asset.current_working_zone_id, self.zone.id)

    def test_end_can_move_the_machine_to_a_different_zone_than_it_started_in(self):
        self._post("start")
        response = self._post("end", extra={"zone_id": self.almacen.id})
        self.assertEqual(response.status_code, 200)
        self.asset.refresh_from_db()
        self.assertEqual(self.asset.current_working_zone_id, self.almacen.id)

    def test_resume_moves_the_machine_when_a_zone_is_sent(self):
        self._post("start")
        self._post("pause")
        response = self._post("resume", extra={"zone_id": self.almacen.id})
        self.assertEqual(response.status_code, 200)
        self.asset.refresh_from_db()
        self.assertEqual(self.asset.current_working_zone_id, self.almacen.id)

    def test_pause_does_not_touch_the_zone_when_nothing_is_sent(self):
        self._post("start", extra={"zone_id": self.almacen.id})
        response = self._post("pause")
        self.assertEqual(response.status_code, 200)
        self.asset.refresh_from_db()
        self.assertEqual(self.asset.current_working_zone_id, self.almacen.id)

    def test_pause_does_not_move_the_zone_from_gps_alone(self):
        self._post("start")
        lat, lon = self.almacen.get_coordinates()
        response = self._post("pause", extra={"latitude": lat, "longitude": lon})
        self.assertEqual(response.status_code, 200)
        self.asset.refresh_from_db()
        self.assertEqual(self.asset.current_working_zone_id, self.zone.id)

    def test_pause_moves_the_zone_when_zone_id_is_sent(self):
        self._post("start")
        response = self._post("pause", extra={"zone_id": self.almacen.id})
        self.assertEqual(response.status_code, 200)
        self.asset.refresh_from_db()
        self.assertEqual(self.asset.current_working_zone_id, self.almacen.id)

    def test_multiple_nearby_zones_still_returns_the_picker(self):
        lat, lon = self.almacen.get_coordinates()
        make_almacen(self.company, name="Almacen 2", lat=lat, lon=lon)
        response = self._post("start", extra={"latitude": lat, "longitude": lon})
        self.assertEqual(response.status_code, 409)
        self.assertTrue(response.json().get("multiple_zones"))
        self.asset.refresh_from_db()
        self.assertEqual(self.asset.current_working_zone_id, self.zone.id)

    def test_an_unknown_zone_id_is_still_a_real_error(self):
        response = self._post("start", extra={"zone_id": 999999})
        self.assertEqual(response.status_code, 400)

    def test_a_zone_cluster_around_the_machines_own_zone_does_not_force_the_picker(self):
        from api.fleet.tests.helpers import make_almacen
        make_almacen(self.company, name="Almacen Vecino", lat=40.406, lon=-3.683)
        lat, lon = self.zone.get_coordinates()

        start_response = self._post("start", extra={"latitude": lat, "longitude": lon})
        self.assertEqual(start_response.status_code, 200)
        self.assertNotIn("multiple_zones", start_response.json())

        pause_response = self._post("pause", extra={"latitude": lat, "longitude": lon})
        self.assertEqual(pause_response.status_code, 200)

        end_response = self._post("end", extra={"latitude": lat, "longitude": lon})
        self.assertEqual(end_response.status_code, 200)
        self.asset.refresh_from_db()
        self.assertEqual(self.asset.current_working_zone_id, self.zone.id)
