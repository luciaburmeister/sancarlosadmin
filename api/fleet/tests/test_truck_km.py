"""
api/fleet/tests/test_truck_km.py

Truck km log: picking a truck and recording its km at clock-in/clock-out,
truck changes, and blocking plannings started in the wrong truck.

Run just this file:
    python manage.py test api.fleet.tests.test_truck_km
"""

import json
from datetime import date, time

from django.test import TestCase, override_settings

from assets.models import (
    AssetIdentifier, AssetPlanning, AssetTypeCategory, PlanningStatus, TruckKmEndReason, TruckKmSession,
)
from company.models import EmployeeType, WorkAttendance
from api.fleet.tests.helpers import (
    ApiFixtureMixin, make_asset, make_asset_type, make_employee, make_user, make_zone,
)

NEW_APP = {"HTTP_X_APP_VERSION": "50"}
OLD_APP = {"HTTP_X_APP_VERSION": "49"}


@override_settings(TRUCK_KM_MIN_APP_VERSION=50)
class TruckKmTestCase(ApiFixtureMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.employee.type = EmployeeType.objects.create(company=self.company, type="Conductor", requires_truck=True)
        self.employee.save()
        truck_type = make_asset_type(self.company, name="CAMIONES", category=AssetTypeCategory.TRUCK)
        self.truck_a = self._truck(truck_type, "MERCEDES A", "1234 ABC")
        self.truck_b = self._truck(truck_type, "VOLVO B", "5678-DEF")

    def _truck(self, truck_type, name, plate):
        truck = make_asset(self.company, truck_type, name=name)
        AssetIdentifier.objects.create(asset=truck, label="MATRICULA", value=plate)
        return truck

    def _post(self, url, data, client=None, **headers):
        client = client or self.api_client
        return client.post(f"/en/api/{url}", data=json.dumps(data), content_type="application/json", **headers)

    def _clock_in(self, **data):
        return self._post("attendance/clock-in/", {"latitude": 40.4, "longitude": -3.7, **data})

    def _clock_out(self, **data):
        return self._post("attendance/clock-out/", {"latitude": 40.4, "longitude": -3.7, **data})


class FichajeTruckKmTests(TruckKmTestCase):
    def test_clock_in_with_truck_and_km_opens_a_session_and_clock_out_closes_it(self):
        response = self._clock_in(truck_id=self.truck_a.id, km=1000)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["open_session"]["truck_id"], self.truck_a.id)
        session = TruckKmSession.objects.get()
        self.assertEqual((session.driver, session.start_km), (self.employee, 1000))
        self.assertEqual(session.attendance, WorkAttendance.objects.get())

        response = self._clock_out(km=1120)
        self.assertEqual(response.status_code, 200)
        session.refresh_from_db()
        self.assertEqual((session.end_km, session.end_reason, session.km_driven), (1120, TruckKmEndReason.CLOCK_OUT, 120))

    def test_old_app_clock_in_and_out_without_truck_still_work(self):
        self.assertEqual(self._clock_in().status_code, 200)
        self.assertEqual(self._clock_out().status_code, 200)
        self.assertFalse(TruckKmSession.objects.exists())

    def test_clock_out_without_km_closes_the_session_flagged_with_no_end_km(self):
        self._clock_in(truck_id=self.truck_a.id, km=1000)
        self._clock_out()
        session = TruckKmSession.objects.get()
        self.assertIsNotNone(session.end_at)
        self.assertIsNone(session.end_km)

    def test_typed_plate_matches_ignoring_spaces_dashes_and_case(self):
        response = self._clock_in(matricula="5678 def", km=10)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(TruckKmSession.objects.get().truck, self.truck_b)

    def test_unknown_plate_is_404_and_does_not_clock_in(self):
        response = self._clock_in(matricula="0000ZZZ", km=10)
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["code"], "truck_not_found")
        self.assertFalse(WorkAttendance.objects.exists())

    def test_status_reports_requires_truck_and_open_session(self):
        self._clock_in(truck_id=self.truck_a.id, km=1000)
        body = self.api_client.get("/en/api/attendance/status/").json()
        self.assertTrue(body["requires_truck"])
        self.assertEqual(body["open_session"]["matricula"], "1234 ABC")


class KmValidationTests(TruckKmTestCase):
    def setUp(self):
        super().setUp()
        TruckKmSession.objects.create(
            company=self.company, truck=self.truck_a, driver=self.employee, start_km=1000,
            start_at="2026-01-01T08:00Z", end_km=1200, end_at="2026-01-01T18:00Z",
        )

    def test_lower_km_than_last_asks_for_confirmation(self):
        response = self._post("truck-km/start/", {"truck_id": self.truck_a.id, "km": 1100})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "km_lower_than_last")
        self.assertEqual(response.json()["last_km"], 1200)

        response = self._post("truck-km/start/", {"truck_id": self.truck_a.id, "km": 1100, "confirm_lower": True})
        self.assertEqual(response.status_code, 200)

    def test_huge_jump_is_rejected_as_a_typo(self):
        response = self._post("truck-km/start/", {"truck_id": self.truck_a.id, "km": 120000})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["code"], "km_invalid")

    def test_trucks_list_shows_last_km_and_filters_by_plate(self):
        body = self.api_client.get("/en/api/trucks/?q=1234").json()
        self.assertEqual(body["trucks"], [
            {"id": self.truck_a.id, "name": "MERCEDES A", "matricula": "1234 ABC", "last_km": 1200},
        ])


class SessionLifecycleTests(TruckKmTestCase):
    def test_start_is_idempotent_for_the_same_truck(self):
        self._post("truck-km/start/", {"truck_id": self.truck_a.id, "km": 10})
        response = self._post("truck-km/start/", {"truck_id": self.truck_a.id, "km": 10})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(TruckKmSession.objects.count(), 1)

    def test_end_with_nothing_open_is_a_no_op(self):
        self.assertEqual(self._post("truck-km/end/", {"km": 10}).status_code, 200)

    def test_switch_closes_the_current_truck_and_opens_the_new_one(self):
        self._post("truck-km/start/", {"truck_id": self.truck_a.id, "km": 100})
        response = self._post("truck-km/switch/", {"end_km": 150, "truck_id": self.truck_b.id, "start_km": 500})
        self.assertEqual(response.status_code, 200)
        old, new = TruckKmSession.objects.order_by("pk")
        self.assertEqual((old.end_km, old.end_reason), (150, TruckKmEndReason.TRUCK_CHANGE))
        self.assertEqual((new.truck, new.start_km, new.end_at), (self.truck_b, 500, None))

    def test_failed_switch_leaves_the_current_truck_open(self):
        self._post("truck-km/start/", {"truck_id": self.truck_a.id, "km": 100})
        response = self._post("truck-km/switch/", {"end_km": 150, "matricula": "NOPE", "start_km": 500})
        self.assertEqual(response.status_code, 404)
        self.assertEqual(TruckKmSession.open_for(self.employee).truck, self.truck_a)

    def test_truck_held_by_another_driver_warns_then_takes_it_over(self):
        other_user = make_user(username="other")
        other = make_employee(self.company, other_user)
        TruckKmSession.objects.create(
            company=self.company, truck=self.truck_a, driver=other, start_km=100, start_at="2026-01-01T08:00Z",
        )
        response = self._post("truck-km/start/", {"truck_id": self.truck_a.id, "km": 150})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "truck_in_use")

        response = self._post("truck-km/start/", {"truck_id": self.truck_a.id, "km": 150, "confirm_takeover": True})
        self.assertEqual(response.status_code, 200)
        taken = TruckKmSession.objects.get(driver=other)
        self.assertEqual((taken.end_km, taken.end_reason), (None, TruckKmEndReason.TAKEN_OVER))


class PlanningTruckCheckTests(TruckKmTestCase):
    def setUp(self):
        super().setUp()
        self.origin = make_zone(self.company, name="Origen")
        self.destination = make_zone(self.company, name="Destino")

    def _porte(self, transport_asset):
        return AssetPlanning.objects.create(
            company=self.company, transport_asset=transport_asset, origin=self.origin, destination=self.destination,
            assigned_to=self.employee, scheduled_date=date.today(), scheduled_time=time(8, 0),
            planning_type="porte", service_type="porte_material", status=PlanningStatus.PENDING,
        )

    def _recogida(self, planning, **headers):
        return self._post(f"portes/{planning.id}/submit/", {"action": "recogida", "matricula": "", "notes": ""}, **headers)

    def test_porte_in_the_wrong_truck_is_blocked_until_a_switch(self):
        self._post("truck-km/start/", {"truck_id": self.truck_a.id, "km": 100})
        planning = self._porte(self.truck_b)

        listed = self.api_client.get("/en/api/portes/").json()["transport_plannings"][0]
        self.assertTrue(listed["truck_mismatch"])
        self.assertEqual(listed["required_truck"]["id"], self.truck_b.id)

        response = self._recogida(planning, **NEW_APP)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "truck_mismatch")
        self.assertEqual(response.json()["current_truck"]["id"], self.truck_a.id)

        self._post("truck-km/switch/", {
            "end_km": 120, "truck_id": self.truck_b.id, "start_km": 500, "planning_id": planning.id,
        })
        self.assertEqual(self._recogida(planning, **NEW_APP).status_code, 200)
        self.assertEqual(TruckKmSession.open_for(self.employee).planning, planning)

    def test_old_app_versions_are_never_blocked(self):
        self._post("truck-km/start/", {"truck_id": self.truck_a.id, "km": 100})
        planning = self._porte(self.truck_b)
        self.assertEqual(self._recogida(planning, **OLD_APP).status_code, 200)

    def test_planning_without_a_truck_is_never_blocked(self):
        planning = self._porte(None)
        self.assertEqual(self._recogida(planning, **NEW_APP).status_code, 200)

    def test_employees_who_do_not_drive_are_never_blocked(self):
        self.employee.type.requires_truck = False
        self.employee.type.save()
        planning = self._porte(self.truck_b)
        self.assertEqual(self._recogida(planning, **NEW_APP).status_code, 200)

    def test_planning_already_under_way_is_not_blocked(self):
        planning = self._porte(self.truck_b)
        planning.status = PlanningStatus.IN_TRANSIT
        planning.save(update_fields=["status"])
        self.assertEqual(self._recogida(planning, **NEW_APP).status_code, 200)

    @override_settings(TRUCK_KM_MIN_APP_VERSION=0)
    def test_check_is_off_when_no_minimum_version_is_configured(self):
        planning = self._porte(self.truck_b)
        self.assertEqual(self._recogida(planning, **NEW_APP).status_code, 200)

    def test_cuba_action_in_the_wrong_truck_is_blocked(self):
        cuba_type = make_asset_type(self.company, name="Cuba 6m3")
        cuba = make_asset(self.company, cuba_type, name="CUBA-1")
        planning = AssetPlanning.objects.create(
            company=self.company, asset=cuba, transport_asset=self.truck_b, assigned_to=self.employee,
            scheduled_date=date.today(), scheduled_time=time(8, 0), planning_type="cuba",
            status=PlanningStatus.PENDING,
        )
        response = self._post(
            f"assets/{cuba.id}/transaction/",
            {"transaction_type": "movement", "planning_id": planning.id}, **NEW_APP,
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "truck_mismatch")
