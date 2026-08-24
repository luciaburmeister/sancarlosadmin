"""
api/fleet/tests/test_pending_cuba_dropoff.py

Covers the "driver ends the day with a cuba still on the truck" flow:
- PlanningNextTaskView.pending_dropoff — surfaced alongside (not instead of) the
  real next task, cleared once resolved or once the next task IS the same cuba.
  Must survive an intervening verteo (RECEIPT) — only DELIVERY resolves it.
- AssetTransactionView's fallback (Asset.pending_pickup_by), letting the driver
  actually close it out (Vertedero/Dejar) even though the planning went
  NOT_COMPLETED and/or its scheduled_date is stale — but not once it's DELIVERED.
- The full "No puedo hacer mas" round trip: PlanningAbandoner marks the cuba
  NOT_COMPLETED, and pending_dropoff must still pick it up on the very next
  /plannings/next/ call, since it never depended on the planning's status.

Run just this file:
    python manage.py test api.fleet.tests.test_pending_cuba_dropoff
"""

import json
from datetime import date, timedelta
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from api.fleet.tests.helpers import make_asset, make_asset_type, make_company, make_employee, make_planning, make_user, make_zone
from assets.models import AssetPlanning, AssetTransaction, PlanningStatus, PlanningType, TransactionTypeChoices
from company.models import WorkingZone


class PlanningNextTaskPendingDropoffTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.company = make_company()
        self.user = make_user()
        self.employee = make_employee(self.company, self.user)
        self.asset_type = make_asset_type(self.company)
        self.obra = make_zone(self.company, name="Obra")
        self.obra.zone_type = WorkingZone.ZoneType.OBRA
        self.obra.save()
        self.client.force_authenticate(user=self.user)

    def _url(self):
        return "/en/api/plannings/next/"

    def _pick_up(self, asset, planning, vas_a_verter="Sí"):
        return AssetTransaction.objects.create(
            company=self.company, asset=asset, from_zone=self.obra,
            transaction_type=TransactionTypeChoices.MOVEMENT, date=timezone.now(),
            performed_by=self.employee, planning=planning, data={"vasAVerter": vas_a_verter},
        )

    def test_no_pending_dropoff_when_nothing_carried(self):
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.json()["pending_dropoff"])

    def test_pending_dropoff_surfaced_alongside_a_different_next_task(self):
        stuck_asset = make_asset(self.company, self.asset_type, employee=self.employee, zone=self.obra, name="CUBA-STUCK")
        stuck_planning = make_planning(
            self.company, asset=stuck_asset, assigned_to=self.employee,
            planning_type=PlanningType.CUBA, status=PlanningStatus.NOT_COMPLETED,
            scheduled_date=date.today() - timedelta(days=1),
        )
        self._pick_up(stuck_asset, stuck_planning, vas_a_verter="Sí")

        other_asset = make_asset(self.company, self.asset_type, employee=self.employee, zone=self.obra, name="CUBA-NEW")
        make_planning(self.company, asset=other_asset, assigned_to=self.employee, planning_type=PlanningType.CUBA)

        response = self.client.get(self._url())
        body = response.json()
        self.assertIsNotNone(body["task"])
        self.assertEqual(body["task"]["asset_id"], other_asset.id)
        self.assertIsNotNone(body["pending_dropoff"])
        self.assertEqual(body["pending_dropoff"]["asset_id"], stuck_asset.id)
        self.assertEqual(body["pending_dropoff"]["planning_id"], stuck_planning.id)
        self.assertTrue(body["pending_dropoff"]["verteo_pending"])

    def test_no_pending_dropoff_when_next_task_resumes_the_same_cuba(self):
        stuck_asset = make_asset(self.company, self.asset_type, employee=self.employee, zone=self.obra, name="CUBA-STUCK")
        stuck_planning = make_planning(
            self.company, asset=stuck_asset, assigned_to=self.employee,
            planning_type=PlanningType.CUBA, status=PlanningStatus.PENDING,
        )
        self._pick_up(stuck_asset, stuck_planning, vas_a_verter="")
        stuck_planning.status = PlanningStatus.IN_TRANSIT
        stuck_planning.save(update_fields=["status"])

        response = self.client.get(self._url())
        body = response.json()
        self.assertEqual(body["task"]["asset_id"], stuck_asset.id)
        self.assertIsNone(body["pending_dropoff"])

    def test_verteo_pending_false_when_no_verteo_was_required(self):
        stuck_asset = make_asset(self.company, self.asset_type, employee=self.employee, zone=self.obra, name="CUBA-STUCK")
        stuck_planning = make_planning(
            self.company, asset=stuck_asset, assigned_to=self.employee,
            planning_type=PlanningType.CUBA, status=PlanningStatus.NOT_COMPLETED,
        )
        self._pick_up(stuck_asset, stuck_planning, vas_a_verter="")

        response = self.client.get(self._url())
        self.assertFalse(response.json()["pending_dropoff"]["verteo_pending"])

    def test_pending_dropoff_omitted_when_last_movement_predates_planning(self):
        # Regression: the Android client parses pending_dropoff.planning_id with
        # JSONObject.getInt(), which throws on JSON null and aborts the whole
        # /plannings/next/ parse — so today's real task never gets painted either,
        # even though it's right there in the same payload. A stuck cuba whose last
        # movement has no planning attached (legacy data from before plan
        # enforcement) must not be surfaced as pending_dropoff at all.
        legacy_asset = make_asset(self.company, self.asset_type, employee=self.employee, zone=self.obra, name="CUBA-LEGACY")
        self._pick_up(legacy_asset, planning=None, vas_a_verter="")

        today_asset = make_asset(self.company, self.asset_type, employee=self.employee, zone=self.obra, name="CUBA-TODAY")
        make_planning(self.company, asset=today_asset, assigned_to=self.employee, planning_type=PlanningType.CUBA)

        response = self.client.get(self._url())
        body = response.json()
        self.assertIsNone(body["pending_dropoff"])
        self.assertIsNotNone(body["task"])
        self.assertEqual(body["task"]["asset_id"], today_asset.id)

    def test_pending_dropoff_survives_a_verteo(self):
        # Regression: the query used to key off the asset's absolute latest
        # transaction, so a RECEIPT (verteo) — which doesn't resolve the pickup —
        # made it disappear from view even though the cuba was still on the truck.
        stuck_asset = make_asset(self.company, self.asset_type, employee=self.employee, zone=self.obra, name="CUBA-STUCK")
        stuck_planning = make_planning(
            self.company, asset=stuck_asset, assigned_to=self.employee,
            planning_type=PlanningType.CUBA, status=PlanningStatus.NOT_COMPLETED,
        )
        self._pick_up(stuck_asset, stuck_planning, vas_a_verter="Sí")
        AssetTransaction.objects.create(
            company=self.company, asset=stuck_asset,
            transaction_type=TransactionTypeChoices.RECEIPT, date=timezone.now(),
            performed_by=self.employee, planning=stuck_planning,
        )

        response = self.client.get(self._url())
        body = response.json()
        self.assertIsNotNone(body["pending_dropoff"])
        self.assertEqual(body["pending_dropoff"]["asset_id"], stuck_asset.id)

    def test_pending_dropoff_clears_once_delivered(self):
        stuck_asset = make_asset(self.company, self.asset_type, employee=self.employee, zone=self.obra, name="CUBA-STUCK")
        stuck_planning = make_planning(
            self.company, asset=stuck_asset, assigned_to=self.employee,
            planning_type=PlanningType.CUBA, status=PlanningStatus.NOT_COMPLETED,
        )
        self._pick_up(stuck_asset, stuck_planning, vas_a_verter="")
        AssetTransaction.objects.create(
            company=self.company, asset=stuck_asset, from_zone=self.obra, to_zone=self.obra,
            transaction_type=TransactionTypeChoices.DELIVERY, date=timezone.now(),
            performed_by=self.employee, planning=stuck_planning,
        )

        response = self.client.get(self._url())
        self.assertIsNone(response.json()["pending_dropoff"])

    def test_pending_dropoff_clears_once_delivered_after_a_verteo(self):
        stuck_asset = make_asset(self.company, self.asset_type, employee=self.employee, zone=self.obra, name="CUBA-STUCK")
        stuck_planning = make_planning(
            self.company, asset=stuck_asset, assigned_to=self.employee,
            planning_type=PlanningType.CUBA, status=PlanningStatus.NOT_COMPLETED,
        )
        self._pick_up(stuck_asset, stuck_planning, vas_a_verter="Sí")
        AssetTransaction.objects.create(
            company=self.company, asset=stuck_asset,
            transaction_type=TransactionTypeChoices.RECEIPT, date=timezone.now(),
            performed_by=self.employee, planning=stuck_planning,
        )
        AssetTransaction.objects.create(
            company=self.company, asset=stuck_asset, from_zone=self.obra, to_zone=self.obra,
            transaction_type=TransactionTypeChoices.DELIVERY, date=timezone.now(),
            performed_by=self.employee, planning=stuck_planning,
        )

        response = self.client.get(self._url())
        self.assertIsNone(response.json()["pending_dropoff"])


class ResumeOwnPendingPickupTests(TestCase):
    """AssetTransactionView's fallback for closing out a stale/abandoned cuba plan."""

    def setUp(self):
        self.client = APIClient()
        self.company = make_company()
        self.user = make_user()
        self.employee = make_employee(self.company, self.user)
        self.asset_type = make_asset_type(self.company)
        self.obra = make_zone(self.company, name="Obra")
        self.obra.zone_type = WorkingZone.ZoneType.OBRA
        self.obra.save()
        self.asset = make_asset(self.company, self.asset_type, employee=self.employee, zone=self.obra)
        self.client.force_authenticate(user=self.user)

    def _url(self):
        return f"/en/api/assets/{self.asset.id}/transaction/"

    def test_delivery_succeeds_on_a_not_completed_stale_dated_plan(self):
        planning = make_planning(
            self.company, asset=self.asset, assigned_to=self.employee,
            planning_type=PlanningType.CUBA, status=PlanningStatus.NOT_COMPLETED,
            scheduled_date=date.today() - timedelta(days=2),
        )
        AssetTransaction.objects.create(
            company=self.company, asset=self.asset, from_zone=self.obra,
            transaction_type=TransactionTypeChoices.MOVEMENT, date=timezone.now(),
            performed_by=self.employee, planning=planning, data={"vasAVerter": ""},
        )
        with patch("api.fleet.views.assets.send_asset_transaction_email_task.delay"):
            response = self.client.post(
                self._url(),
                data=json.dumps({
                    "transaction_type": "delivery",
                    "latitude": 40.416775, "longitude": -3.703790,
                    "zone_id": self.obra.id, "planning_id": planning.id,
                }),
                content_type="application/json",
            )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json().get("success"))
        self.assertTrue(AssetTransaction.objects.filter(transaction_type=TransactionTypeChoices.DELIVERY).exists())

    def test_delivery_still_rejected_when_someone_else_holds_it(self):
        other_user = make_user(username="other")
        other_employee = make_employee(self.company, other_user)
        planning = make_planning(
            self.company, asset=self.asset, assigned_to=self.employee,
            planning_type=PlanningType.CUBA, status=PlanningStatus.NOT_COMPLETED,
            scheduled_date=date.today() - timedelta(days=2),
        )
        AssetTransaction.objects.create(
            company=self.company, asset=self.asset, from_zone=self.obra,
            transaction_type=TransactionTypeChoices.MOVEMENT, date=timezone.now(),
            performed_by=other_employee, planning=planning, data={"vasAVerter": ""},
        )
        response = self.client.post(
            self._url(),
            data=json.dumps({
                "transaction_type": "delivery",
                "latitude": 40.416775, "longitude": -3.703790,
                "zone_id": self.obra.id, "planning_id": planning.id,
            }),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)

    def test_receipt_rejected_once_the_cuba_was_already_delivered(self):
        # Regression: pending_pickup_by used to check current_transaction_status of
        # the MOVEMENT itself, which is tautologically always PICKED_UP — so a plan
        # stayed "resumable" forever, even after a real DELIVERY closed it out.
        planning = make_planning(
            self.company, asset=self.asset, assigned_to=self.employee,
            planning_type=PlanningType.CUBA, status=PlanningStatus.NOT_COMPLETED,
            scheduled_date=date.today() - timedelta(days=2),
        )
        AssetTransaction.objects.create(
            company=self.company, asset=self.asset, from_zone=self.obra,
            transaction_type=TransactionTypeChoices.MOVEMENT, date=timezone.now() - timedelta(minutes=10),
            performed_by=self.employee, planning=planning, data={"vasAVerter": "Sí"},
        )
        AssetTransaction.objects.create(
            company=self.company, asset=self.asset, from_zone=self.obra, to_zone=self.obra,
            transaction_type=TransactionTypeChoices.DELIVERY, date=timezone.now(),
            performed_by=self.employee, planning=planning,
        )
        response = self.client.post(
            self._url(),
            data=json.dumps({
                "transaction_type": "receipt",
                "nro_albaran": "ALB-002", "latitude": 40.416775, "longitude": -3.703790,
                "zone_id": self.obra.id, "planning_id": planning.id,
            }),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)


class AbandonThenNextTaskEndToEndTests(TestCase):
    """The actual "No puedo hacer mas" round trip: hit /plannings/abandon/ for
    real, then /plannings/next/, instead of hand-crafting a NOT_COMPLETED
    planning directly."""

    def setUp(self):
        self.client = APIClient()
        self.company = make_company()
        self.user = make_user()
        self.employee = make_employee(self.company, self.user)
        self.asset_type = make_asset_type(self.company)
        self.obra = make_zone(self.company, name="Obra")
        self.obra.zone_type = WorkingZone.ZoneType.OBRA
        self.obra.save()
        self.client.force_authenticate(user=self.user)

    def test_pending_dropoff_appears_right_after_abandoning_a_full_cuba(self):
        stuck_asset = make_asset(self.company, self.asset_type, employee=self.employee, zone=self.obra, name="CUBA-STUCK")
        stuck_planning = make_planning(
            self.company, asset=stuck_asset, assigned_to=self.employee,
            planning_type=PlanningType.CUBA, status=PlanningStatus.IN_TRANSIT,
        )
        AssetTransaction.objects.create(
            company=self.company, asset=stuck_asset, from_zone=self.obra,
            transaction_type=TransactionTypeChoices.MOVEMENT, date=timezone.now(),
            performed_by=self.employee, planning=stuck_planning, data={"vasAVerter": "Sí"},
        )

        abandon_response = self.client.post(
            "/en/api/plannings/abandon/",
            data=json.dumps({"reason": "Fin de jornada laboral"}),
            content_type="application/json",
        )
        self.assertEqual(abandon_response.status_code, 200)
        stuck_planning.refresh_from_db()
        self.assertEqual(stuck_planning.status, PlanningStatus.NOT_COMPLETED)

        next_response = self.client.get("/en/api/plannings/next/")
        body = next_response.json()
        self.assertIsNone(body["task"])
        self.assertIsNotNone(body["pending_dropoff"])
        self.assertEqual(body["pending_dropoff"]["asset_id"], stuck_asset.id)
        self.assertTrue(body["pending_dropoff"]["verteo_pending"])

    def test_pending_dropoff_still_blocks_an_unrelated_task_assigned_after_abandoning(self):
        stuck_asset = make_asset(self.company, self.asset_type, employee=self.employee, zone=self.obra, name="CUBA-STUCK")
        stuck_planning = make_planning(
            self.company, asset=stuck_asset, assigned_to=self.employee,
            planning_type=PlanningType.CUBA, status=PlanningStatus.IN_TRANSIT,
        )
        AssetTransaction.objects.create(
            company=self.company, asset=stuck_asset, from_zone=self.obra,
            transaction_type=TransactionTypeChoices.MOVEMENT, date=timezone.now(),
            performed_by=self.employee, planning=stuck_planning, data={"vasAVerter": ""},
        )
        self.client.post(
            "/en/api/plannings/abandon/",
            data=json.dumps({}), content_type="application/json",
        )

        # Dispatch assigns a brand new, unrelated cuba for the next day.
        other_asset = make_asset(self.company, self.asset_type, employee=self.employee, zone=self.obra, name="CUBA-NEW")
        make_planning(self.company, asset=other_asset, assigned_to=self.employee, planning_type=PlanningType.CUBA)

        body = self.client.get("/en/api/plannings/next/").json()
        self.assertEqual(body["task"]["asset_id"], other_asset.id)
        self.assertIsNotNone(body["pending_dropoff"])
        self.assertEqual(body["pending_dropoff"]["asset_id"], stuck_asset.id)
