"""
api/fleet/tests/test_next_day_task.py

/plannings/next/ previews the next scheduled day's first plan ("next_day_task")
once today's queue is empty, and every driver action endpoint refuses a plan
scheduled after today (400, code "future_planning").
"""
import json
from datetime import time, timedelta

from django.test import TestCase
from django.utils import timezone

from api.fleet.future_planning import FUTURE_PLANNING_CODE
from api.fleet.tests.helpers import (
    ApiFixtureMixin, make_asset, make_asset_type, make_planning, make_zone,
)
from assets.models import AssetPlanning, AssetTransaction, PlanningStatus, PlanningType

NEXT_URL = "/en/api/plannings/next/"


class NextDayTaskTests(ApiFixtureMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.today = timezone.localdate()

    def _plan(self, days=0, **kwargs):
        return make_planning(
            self.company, assigned_to=self.employee,
            scheduled_date=self.today + timedelta(days=days), **kwargs,
        )

    def _next(self):
        response = self.client.get(NEXT_URL)
        self.assertEqual(response.status_code, 200)
        return response.json()

    def test_null_while_today_still_has_a_task(self):
        today_plan = self._plan()
        self._plan(days=1)
        body = self._next()
        self.assertEqual(body["task"]["id"], today_plan.id)
        self.assertIsNone(body["next_day_task"])

    def test_shows_first_plan_of_the_earliest_future_day(self):
        self._plan(status=PlanningStatus.DELIVERED)
        self._plan(days=4, scheduled_time=time(6, 0))
        later = self._plan(days=3, scheduled_time=time(11, 0), planning_type=PlanningType.PORTE)
        first = self._plan(days=3, scheduled_time=time(7, 30), planning_type=PlanningType.PORTE)
        body = self._next()
        self.assertIsNone(body["task"])
        task = body["next_day_task"]
        self.assertEqual(task["id"], first.id)
        self.assertNotEqual(task["id"], later.id)
        self.assertEqual(task["scheduled_date"], first.scheduled_date.strftime("%d/%m/%Y"))
        self.assertEqual(task["scheduled_time"], "07:30")
        self.assertEqual(task["planning_type"], PlanningType.PORTE)

    def test_route_order_breaks_ties_at_the_same_time(self):
        self._plan(days=1, route_order=2)
        first = self._plan(days=1, route_order=1)
        self.assertEqual(self._next()["next_day_task"]["id"], first.id)

    def test_ignores_non_pending_unscheduled_and_other_drivers_plans(self):
        self._plan(days=1, status=PlanningStatus.NOT_COMPLETED)
        self._plan(days=1, scheduled_time=None)
        make_planning(self.company, scheduled_date=self.today + timedelta(days=1))
        self.assertIsNone(self._next()["next_day_task"])

    def test_cuba_serializer_also_carries_scheduled_time(self):
        plan = self._plan(scheduled_time=time(9, 5))
        self.assertEqual(self._next()["task"]["scheduled_time"], "09:05")
        self.assertEqual(plan.planning_type, PlanningType.CUBA)


class FuturePlanningBlockedTests(ApiFixtureMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.tomorrow = timezone.localdate() + timedelta(days=1)
        self.zone = make_zone(self.company)
        self.asset = make_asset(self.company, make_asset_type(self.company), zone=self.zone)

    def _future(self, **kwargs):
        return make_planning(
            self.company, assigned_to=self.employee, scheduled_date=self.tomorrow,
            origin=self.zone, destination=self.zone, **kwargs,
        )

    def _assert_blocked(self, response):
        self.assertEqual(response.status_code, 400)
        body = response.json()
        self.assertEqual(body["code"], FUTURE_PLANNING_CODE)
        self.assertEqual(
            body["error"],
            f"Esta tarea es de {self.tomorrow.strftime('%d/%m/%Y')}. No puedes empezarla hoy.",
        )

    def test_porte_submit(self):
        planning = self._future(planning_type=PlanningType.PORTE)
        response = self.client.post(
            f"/en/api/portes/{planning.id}/submit/",
            {"action": "recogida", "firma": "", "imagen": "", "matricula": "", "notes": ""},
            format="json",
        )
        self._assert_blocked(response)
        planning.refresh_from_db()
        self.assertEqual(planning.status, PlanningStatus.PENDING)
        self.assertFalse(AssetTransaction.objects.exists())

    def test_asset_transaction_with_future_planning_id(self):
        planning = self._future(asset=self.asset)
        response = self.client.post(
            f"/en/api/assets/{self.asset.id}/transaction/",
            data=json.dumps({
                "transaction_type": "movement", "planning_id": planning.id,
                "zone_id": self.zone.id, "latitude": 40.416775, "longitude": -3.703790,
            }),
            content_type="application/json",
        )
        self._assert_blocked(response)
        self.assertFalse(AssetTransaction.objects.exists())

    def test_reserve_cuba(self):
        planning = self._future()
        response = self.client.post(
            f"/en/api/plannings/{planning.id}/reserve-cuba/", {"asset_id": self.asset.id}, format="json",
        )
        self._assert_blocked(response)
        self.assertIsNone(AssetPlanning.objects.get(pk=planning.pk).reserved_asset_id)

    def test_cuba_suggestions(self):
        planning = self._future()
        self._assert_blocked(self.client.get(f"/en/api/plannings/{planning.id}/cuba-suggestions/"))

    def test_assign_never_picks_a_future_plan(self):
        planning = self._future()
        response = self.client.post(f"/en/api/assets/{self.asset.id}/assign/", {}, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertIsNone(AssetPlanning.objects.get(pk=planning.pk).asset_id)
