"""
api/fleet/tests/test_pending_cuba_dropoff.py

Covers the "driver still has a cuba needing attention" flow:
- PlanningNextTaskView.pending_dropoff — surfaced alongside (not instead of) the
  real next task, whenever EITHER of two independent things is true for some
  asset: (a) it's still owed a verteo (with_verteo_pending_by — survives a
  DELIVERY on purpose, since a full cuba can be dropped at an almacén and still
  need verting, only a RECEIPT resolves this one), or (b) it's still physically
  on the truck at all (with_pending_pickup_by — full or empty, only a DELIVERY
  resolves this one, a RECEIPT alone doesn't). (a) takes priority when both
  would somehow point at different assets. Carries verteo_pending (Vertedero
  required before anything else) and can_use_for_next_task (only true once
  verteo isn't pending, there's an actual next job that can take this cuba —
  none assigned yet, or the same leftover asset pre-pinned by dispatch — and
  the driver hasn't already claimed it). Dispatch matching asset_id is not an
  auto-resume; "Usar esta cuba" must be explicit.
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
from assets.models import (
    AssetPlanning, AssetTransaction, ChecklistStepKey, CubaOperationType,
    PlanningStatus, PlanningType, PorteAction, PorteMovementKind,
    PorteTransactionDataKey, TransactionTypeChoices,
)
from assets.constants import AssignRequestKey
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
        # Still owes a verteo — can't be reassigned to the other cuba's job yet,
        # regardless of that job having no asset of its own.
        self.assertFalse(body["pending_dropoff"]["can_use_for_next_task"])

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

    def test_non_full_pickup_still_surfaces_but_without_verteo_pending(self):
        # Empty pickup already closed its own plan on the recogida (see
        # test_strategies.MovementStrategyTests) — but the cuba is still
        # physically on the truck, undelivered, so it must still show up here,
        # just without the verteo-pending flag forcing Vertedero.
        stuck_asset = make_asset(self.company, self.asset_type, employee=self.employee, zone=self.obra, name="CUBA-STUCK")
        stuck_planning = make_planning(
            self.company, asset=stuck_asset, assigned_to=self.employee,
            planning_type=PlanningType.CUBA, status=PlanningStatus.DELIVERED,
        )
        self._pick_up(stuck_asset, stuck_planning, vas_a_verter="")

        response = self.client.get(self._url())
        pending = response.json()["pending_dropoff"]
        self.assertIsNotNone(pending)
        self.assertEqual(pending["asset_id"], stuck_asset.id)
        self.assertFalse(pending["verteo_pending"])

    def test_can_use_for_next_task_true_when_next_cuba_job_has_no_asset_yet(self):
        stuck_asset = make_asset(self.company, self.asset_type, employee=self.employee, zone=self.obra, name="CUBA-STUCK")
        stuck_planning = make_planning(
            self.company, asset=stuck_asset, assigned_to=self.employee,
            planning_type=PlanningType.CUBA, status=PlanningStatus.DELIVERED,
        )
        self._pick_up(stuck_asset, stuck_planning, vas_a_verter="")
        # Next job is unassigned — dispatch didn't pin a specific cuba to it.
        make_planning(self.company, asset=None, assigned_to=self.employee, planning_type=PlanningType.CUBA)

        response = self.client.get(self._url())
        self.assertTrue(response.json()["pending_dropoff"]["can_use_for_next_task"])

    def test_can_use_for_next_task_false_when_next_job_already_has_a_different_asset(self):
        stuck_asset = make_asset(self.company, self.asset_type, employee=self.employee, zone=self.obra, name="CUBA-STUCK")
        stuck_planning = make_planning(
            self.company, asset=stuck_asset, assigned_to=self.employee,
            planning_type=PlanningType.CUBA, status=PlanningStatus.DELIVERED,
        )
        self._pick_up(stuck_asset, stuck_planning, vas_a_verter="")
        other_asset = make_asset(self.company, self.asset_type, employee=self.employee, zone=self.obra, name="CUBA-OTHER")
        make_planning(self.company, asset=other_asset, assigned_to=self.employee, planning_type=PlanningType.CUBA)

        response = self.client.get(self._url())
        self.assertFalse(response.json()["pending_dropoff"]["can_use_for_next_task"])

    def test_can_use_for_next_task_true_when_next_job_is_a_porte(self):
        """A cuba picked up on a cuba job can also feed a porte's recogida —
        the shortcut isn't cuba-to-cuba only."""
        stuck_asset = make_asset(self.company, self.asset_type, employee=self.employee, zone=self.obra, name="CUBA-STUCK")
        stuck_planning = make_planning(
            self.company, asset=stuck_asset, assigned_to=self.employee,
            planning_type=PlanningType.CUBA, status=PlanningStatus.DELIVERED,
        )
        self._pick_up(stuck_asset, stuck_planning, vas_a_verter="")
        make_planning(self.company, asset=None, assigned_to=self.employee, planning_type=PlanningType.PORTE)

        response = self.client.get(self._url())
        self.assertTrue(response.json()["pending_dropoff"]["can_use_for_next_task"])

    def test_can_use_for_next_task_false_when_there_is_no_next_task_at_all(self):
        stuck_asset = make_asset(self.company, self.asset_type, employee=self.employee, zone=self.obra, name="CUBA-STUCK")
        stuck_planning = make_planning(
            self.company, asset=stuck_asset, assigned_to=self.employee,
            planning_type=PlanningType.CUBA, status=PlanningStatus.DELIVERED,
        )
        self._pick_up(stuck_asset, stuck_planning, vas_a_verter="")

        response = self.client.get(self._url())
        body = response.json()
        self.assertIsNone(body["task"])
        self.assertFalse(body["pending_dropoff"]["can_use_for_next_task"])

    def test_dispatch_preassigned_same_cuba_does_not_auto_resume(self):
        """Office pinning the leftover cuba onto the next job must not hide the
        pending card — the driver still has to mark 'usar esta cuba' or leave it."""
        stuck_asset = make_asset(self.company, self.asset_type, employee=self.employee, zone=self.obra, name="CUBA-STUCK")
        stuck_planning = make_planning(
            self.company, asset=stuck_asset, assigned_to=self.employee,
            planning_type=PlanningType.CUBA, status=PlanningStatus.DELIVERED,
        )
        self._pick_up(stuck_asset, stuck_planning, vas_a_verter="")
        next_plan = make_planning(
            self.company, asset=stuck_asset, assigned_to=self.employee,
            planning_type=PlanningType.CUBA, cuba_operation_type=CubaOperationType.PUESTA,
        )

        body = self.client.get(self._url()).json()
        self.assertEqual(body["task"]["id"], next_plan.id)
        self.assertIsNotNone(body["pending_dropoff"])
        self.assertEqual(body["pending_dropoff"]["asset_id"], stuck_asset.id)
        self.assertTrue(body["pending_dropoff"]["can_use_for_next_task"])
        self.assertEqual(body["task"]["checklist"][0]["key"], ChecklistStepKey.RECOGER_CUBA)
        self.assertEqual(body["task"]["checklist"][0]["status"], "pending")

    def test_usar_esta_cuba_claims_the_next_plan_and_hides_pending(self):
        stuck_asset = make_asset(self.company, self.asset_type, employee=self.employee, zone=self.obra, name="CUBA-STUCK")
        stuck_planning = make_planning(
            self.company, asset=stuck_asset, assigned_to=self.employee,
            planning_type=PlanningType.CUBA, status=PlanningStatus.DELIVERED,
        )
        self._pick_up(stuck_asset, stuck_planning, vas_a_verter="")
        next_plan = make_planning(
            self.company, asset=None, assigned_to=self.employee,
            planning_type=PlanningType.CUBA, cuba_operation_type=CubaOperationType.PUESTA,
        )

        assign = self.client.post(
            f"/en/api/assets/{stuck_asset.id}/assign/",
            data=json.dumps({AssignRequestKey.USE_PENDING_CUBA: True}),
            content_type="application/json",
        )
        self.assertEqual(assign.status_code, 200)
        next_plan.refresh_from_db()
        self.assertEqual(next_plan.asset_id, stuck_asset.id)
        self.assertTrue(next_plan.driver_claimed_cuba)
        self.assertEqual(next_plan.status, PlanningStatus.IN_TRANSIT)

        body = self.client.get(self._url()).json()
        self.assertEqual(body["task"]["id"], next_plan.id)
        self.assertEqual(body["task"]["status"], PlanningStatus.IN_TRANSIT)
        self.assertIsNone(body["pending_dropoff"])
        self.assertEqual(body["task"]["checklist"][0]["status"], "done")

    def test_verteo_on_a_puesta_does_not_auto_claim_the_emptied_cuba(self):
        """Jose Maria case: picking up full on a puesta, then verting, used to
        keep the emptied cuba silently bound to that plan. It must surface the
        pending card so the driver chooses usar or dejar."""
        from api.fleet.views.assets import ReceiptStrategy

        asset = make_asset(self.company, self.asset_type, employee=self.employee, zone=self.obra, name="CUBA-13")
        puesta = make_planning(
            self.company, asset=asset, assigned_to=self.employee,
            planning_type=PlanningType.CUBA, cuba_operation_type=CubaOperationType.PUESTA,
            status=PlanningStatus.IN_TRANSIT,
        )
        self._pick_up(asset, puesta, vas_a_verter="Sí")
        ReceiptStrategy(
            {
                "transaction_type": "receipt", "nro_albaran": "ALB-1", "foto_albaran": "",
                "peso_neto": "500", "material_vertido": "RCD Limpio",
                "zone_id": self.obra.id, "planning_id": puesta.id,
            },
            asset, self.employee, self.user,
        ).execute()
        puesta.refresh_from_db()
        self.assertFalse(puesta.driver_claimed_cuba)

        body = self.client.get(self._url()).json()
        self.assertEqual(body["task"]["id"], puesta.id)
        self.assertIsNotNone(body["pending_dropoff"])
        self.assertFalse(body["pending_dropoff"]["verteo_pending"])
        self.assertTrue(body["pending_dropoff"]["can_use_for_next_task"])

    def test_leaving_leftover_does_not_mark_next_plan_as_already_collected(self):
        stuck_asset = make_asset(self.company, self.asset_type, employee=self.employee, zone=self.obra, name="CUBA-STUCK")
        stuck_planning = make_planning(
            self.company, asset=stuck_asset, assigned_to=self.employee,
            planning_type=PlanningType.CUBA, status=PlanningStatus.DELIVERED,
        )
        self._pick_up(stuck_asset, stuck_planning, vas_a_verter="")
        AssetTransaction.objects.create(
            company=self.company, asset=stuck_asset, from_zone=self.obra, to_zone=self.obra,
            transaction_type=TransactionTypeChoices.DELIVERY, date=timezone.now(),
            performed_by=self.employee, planning=stuck_planning,
        )
        next_plan = make_planning(
            self.company, asset=stuck_asset, assigned_to=self.employee,
            planning_type=PlanningType.CUBA, cuba_operation_type=CubaOperationType.RETIRADA,
        )

        body = self.client.get(self._url()).json()
        self.assertEqual(body["task"]["id"], next_plan.id)
        self.assertIsNone(body["pending_dropoff"])
        recoger = next(s for s in body["task"]["checklist"] if s["key"] == ChecklistStepKey.RECOGER_CUBA)
        self.assertEqual(recoger["status"], "pending")

    def test_verteo_does_not_attach_leftover_cuba_to_the_next_unassigned_plan(self):
        from api.fleet.views.assets import ReceiptStrategy

        stuck_asset = make_asset(self.company, self.asset_type, employee=self.employee, zone=self.obra, name="CUBA-STUCK")
        stuck_planning = make_planning(
            self.company, asset=stuck_asset, assigned_to=self.employee,
            planning_type=PlanningType.CUBA, status=PlanningStatus.IN_TRANSIT,
        )
        self._pick_up(stuck_asset, stuck_planning, vas_a_verter="Sí")
        next_plan = make_planning(
            self.company, asset=None, assigned_to=self.employee,
            planning_type=PlanningType.CUBA, cuba_operation_type=CubaOperationType.PUESTA,
        )

        response = ReceiptStrategy(
            {
                "transaction_type": "receipt", "nro_albaran": "ALB-1", "foto_albaran": "",
                "peso_neto": "500", "material_vertido": "RCD Limpio",
                "zone_id": self.obra.id, "planning_id": next_plan.id,
            },
            stuck_asset, self.employee, self.user,
        ).execute()
        self.assertEqual(response.status_code, 400)
        next_plan.refresh_from_db()
        self.assertIsNone(next_plan.asset_id)
        self.assertFalse(next_plan.driver_claimed_cuba)

    def test_pending_dropoff_omitted_when_last_movement_predates_planning(self):
        # Regression: the Android client parses pending_dropoff.planning_id with
        # JSONObject.getInt(), which throws on JSON null and aborts the whole
        # /plannings/next/ parse — so today's real task never gets painted either,
        # even though it's right there in the same payload. A stuck cuba whose last
        # movement has no planning attached (legacy data from before plan
        # enforcement) must not be surfaced as pending_dropoff at all.
        legacy_asset = make_asset(self.company, self.asset_type, employee=self.employee, zone=self.obra, name="CUBA-LEGACY")
        self._pick_up(legacy_asset, planning=None, vas_a_verter="Sí")

        today_asset = make_asset(self.company, self.asset_type, employee=self.employee, zone=self.obra, name="CUBA-TODAY")
        make_planning(self.company, asset=today_asset, assigned_to=self.employee, planning_type=PlanningType.CUBA)

        response = self.client.get(self._url())
        body = response.json()
        self.assertIsNone(body["pending_dropoff"])
        self.assertIsNotNone(body["task"])
        self.assertEqual(body["task"]["asset_id"], today_asset.id)

    def test_pending_dropoff_survives_a_verteo_but_loses_verteo_pending(self):
        # Verting closes the cuba's own plan (see ReceiptStrategyTests) and clears
        # verteo_pending — but the cuba is still physically on the truck until an
        # actual DELIVERY happens, so it must keep surfacing here, just no longer
        # forcing Vertedero.
        stuck_asset = make_asset(self.company, self.asset_type, employee=self.employee, zone=self.obra, name="CUBA-STUCK")
        stuck_planning = make_planning(
            # Already DELIVERED, same as ReceiptStrategy would leave it right after
            # this verteo (see ReceiptStrategyTests) — isolates pending_dropoff's own
            # computation from the separate "resuming the same task" suppression.
            self.company, asset=stuck_asset, assigned_to=self.employee,
            planning_type=PlanningType.CUBA, status=PlanningStatus.DELIVERED,
        )
        self._pick_up(stuck_asset, stuck_planning, vas_a_verter="Sí")
        AssetTransaction.objects.create(
            company=self.company, asset=stuck_asset,
            transaction_type=TransactionTypeChoices.RECEIPT, date=timezone.now(),
            performed_by=self.employee, planning=stuck_planning,
        )

        response = self.client.get(self._url())
        pending = response.json()["pending_dropoff"]
        self.assertIsNotNone(pending)
        self.assertEqual(pending["asset_id"], stuck_asset.id)
        self.assertFalse(pending["verteo_pending"])

    def test_pending_dropoff_survives_a_delivery_when_verteo_still_pending(self):
        # Regression: dropping a full cuba off at an almacén (allowed — see
        # DeliveryStrategyTests) used to clear pending_dropoff entirely, because
        # with_pending_pickup_by (physically-on-the-truck) resolves on any
        # DELIVERY. But the verteo is still owed, so this must keep surfacing,
        # with verteo_pending still true — nothing has been verted yet.
        stuck_asset = make_asset(self.company, self.asset_type, employee=self.employee, zone=self.obra, name="CUBA-STUCK2")
        stuck_planning = make_planning(
            # DeliveryStrategy closes the plan on zone match regardless of verteo
            # state (see DeliveryStrategyTests) — isolate pending_dropoff's own
            # computation from the separate "resuming the same task" suppression.
            self.company, asset=stuck_asset, assigned_to=self.employee,
            planning_type=PlanningType.CUBA, status=PlanningStatus.DELIVERED,
        )
        self._pick_up(stuck_asset, stuck_planning, vas_a_verter="Sí")
        almacen = make_zone(self.company, name="Almacen")
        almacen.zone_type = WorkingZone.ZoneType.ALMACEN
        almacen.save()
        AssetTransaction.objects.create(
            company=self.company, asset=stuck_asset, from_zone=self.obra, to_zone=almacen,
            transaction_type=TransactionTypeChoices.DELIVERY, date=timezone.now(),
            performed_by=self.employee, planning=stuck_planning, data={"cubaEstado": "Cuba llena"},
        )

        response = self.client.get(self._url())
        pending = response.json()["pending_dropoff"]
        self.assertIsNotNone(pending)
        self.assertEqual(pending["asset_id"], stuck_asset.id)
        self.assertTrue(pending["verteo_pending"])

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

    def test_receipt_still_allowed_after_leaving_a_full_cuba_at_a_warehouse(self):
        # A full cuba dropped at an almacén still owes its verteo — the Home
        # card keeps showing Vertedero, so the receipt must resume that plan
        # even though the cuba is no longer PICKED_UP.
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
        self.assertEqual(response.status_code, 200)
        self.assertTrue(AssetTransaction.objects.filter(transaction_type=TransactionTypeChoices.RECEIPT).exists())

    def test_receipt_rejected_after_delivery_when_no_verteo_is_owed(self):
        planning = make_planning(
            self.company, asset=self.asset, assigned_to=self.employee,
            planning_type=PlanningType.CUBA, status=PlanningStatus.NOT_COMPLETED,
            scheduled_date=date.today() - timedelta(days=2),
        )
        AssetTransaction.objects.create(
            company=self.company, asset=self.asset, from_zone=self.obra,
            transaction_type=TransactionTypeChoices.MOVEMENT, date=timezone.now() - timedelta(minutes=10),
            performed_by=self.employee, planning=planning, data={"vasAVerter": ""},
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
            data=json.dumps({"planning_id": stuck_planning.id, "reason": "Fin de jornada laboral"}),
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
            performed_by=self.employee, planning=stuck_planning, data={"vasAVerter": "Sí"},
        )
        self.client.post(
            "/en/api/plannings/abandon/",
            data=json.dumps({"planning_id": stuck_planning.id, "reason": "Fin de jornada laboral"}),
            content_type="application/json",
        )

        # Dispatch assigns a brand new, unrelated cuba for the next day.
        other_asset = make_asset(self.company, self.asset_type, employee=self.employee, zone=self.obra, name="CUBA-NEW")
        make_planning(self.company, asset=other_asset, assigned_to=self.employee, planning_type=PlanningType.CUBA)

        body = self.client.get("/en/api/plannings/next/").json()
        self.assertEqual(body["task"]["asset_id"], other_asset.id)
        self.assertIsNotNone(body["pending_dropoff"])
        self.assertEqual(body["pending_dropoff"]["asset_id"], stuck_asset.id)


class PendingDropoffIncludesPorteTests(TestCase):
    """A cuba picked up via a porte's recogida (PORTE_MOVEMENT) must surface
    the same way a cuba-recogida one does, and can feed either a cuba or a
    porte next task."""

    def setUp(self):
        self.client = APIClient()
        self.company = make_company()
        self.user = make_user()
        self.employee = make_employee(self.company, self.user)
        self.asset_type = make_asset_type(self.company)
        self.obra = make_zone(self.company, name="Obra")
        self.obra.zone_type = WorkingZone.ZoneType.OBRA
        self.obra.save()
        self.destino = make_zone(self.company, name="Destino")
        self.client.force_authenticate(user=self.user)

    def _porte_recogida(self, asset, planning):
        return AssetTransaction.objects.create(
            company=self.company, asset=asset, from_zone=self.obra,
            transaction_type=TransactionTypeChoices.PORTE_MOVEMENT, date=timezone.now(),
            performed_by=self.employee, planning=planning,
            data={
                PorteTransactionDataKey.MOVEMENT_TYPE: PorteMovementKind.PORTE,
                PorteTransactionDataKey.ACTION: PorteAction.RECOGIDA,
            },
        )

    def test_porte_recogida_surfaces_as_pending_dropoff(self):
        """A separate, already-abandoned porte (not today's active task) whose
        cuba is still on the truck — mirrors
        test_pending_dropoff_surfaced_alongside_a_different_next_task."""
        cuba = make_asset(self.company, self.asset_type, employee=self.employee, zone=self.obra, name="CUBA-PORTE")
        porte = make_planning(
            self.company, asset=cuba, assigned_to=self.employee, planning_type=PlanningType.PORTE,
            service_type="porte_material", origin=self.obra, destination=self.destino,
            status=PlanningStatus.NOT_COMPLETED, scheduled_date=date.today() - timedelta(days=1),
        )
        self._porte_recogida(cuba, porte)

        body = self.client.get(self._url()).json()
        self.assertIsNotNone(body["pending_dropoff"])
        self.assertEqual(body["pending_dropoff"]["asset_id"], cuba.id)
        self.assertEqual(body["pending_dropoff"]["planning_id"], porte.id)
        self.assertFalse(body["pending_dropoff"]["verteo_pending"])

    def test_can_use_for_next_task_true_when_next_task_is_a_porte(self):
        cuba = make_asset(self.company, self.asset_type, employee=self.employee, zone=self.obra, name="CUBA-PORTE")
        old_porte = make_planning(
            self.company, asset=cuba, assigned_to=self.employee, planning_type=PlanningType.PORTE,
            service_type="porte_material", origin=self.obra, destination=self.destino,
            status=PlanningStatus.DELIVERED,
        )
        self._porte_recogida(cuba, old_porte)

        next_porte = make_planning(
            self.company, asset=None, assigned_to=self.employee, planning_type=PlanningType.PORTE,
            service_type="porte_material", origin=self.obra, destination=self.destino,
        )

        body = self.client.get(self._url()).json()
        self.assertEqual(body["task"]["id"], next_porte.id)
        self.assertIsNotNone(body["pending_dropoff"])
        self.assertTrue(body["pending_dropoff"]["can_use_for_next_task"])

    def test_still_pending_after_carga_step_load_is_transparent(self):
        """After recogida + carga (PORTE_LOAD), the cuba must still read as
        'still on the truck' — carga isn't a dropoff."""
        cuba = make_asset(self.company, self.asset_type, employee=self.employee, zone=self.obra, name="CUBA-PORTE")
        porte = make_planning(
            self.company, asset=cuba, assigned_to=self.employee, planning_type=PlanningType.PORTE,
            service_type="porte_material", origin=self.obra, destination=self.destino,
            status=PlanningStatus.NOT_COMPLETED, scheduled_date=date.today() - timedelta(days=1),
        )
        self._porte_recogida(cuba, porte)
        AssetTransaction.objects.create(
            company=self.company, asset=cuba, from_zone=self.obra, to_zone=self.destino,
            transaction_type=TransactionTypeChoices.PORTE_LOAD, date=timezone.now(),
            performed_by=self.employee, planning=porte,
            data={
                PorteTransactionDataKey.MOVEMENT_TYPE: PorteMovementKind.PORTE,
                PorteTransactionDataKey.ACTION: PorteAction.CARGA,
            },
        )

        body = self.client.get(self._url()).json()
        self.assertIsNotNone(body["pending_dropoff"])
        self.assertEqual(body["pending_dropoff"]["asset_id"], cuba.id)

    def test_still_pending_after_porte_delivery(self):
        """Porte entrega leaves the empty cuba on the truck for the next job."""
        cuba = make_asset(self.company, self.asset_type, employee=self.employee, zone=self.obra, name="CUBA-PORTE")
        porte = make_planning(
            self.company, asset=cuba, assigned_to=self.employee, planning_type=PlanningType.PORTE,
            service_type="porte_material", origin=self.obra, destination=self.destino,
        )
        self._porte_recogida(cuba, porte)
        AssetTransaction.objects.create(
            company=self.company, asset=cuba, from_zone=self.obra, to_zone=self.destino,
            transaction_type=TransactionTypeChoices.PORTE_DELIVERY, date=timezone.now(),
            performed_by=self.employee, planning=porte,
            data={
                PorteTransactionDataKey.MOVEMENT_TYPE: PorteMovementKind.PORTE,
                PorteTransactionDataKey.ACTION: PorteAction.ENTREGA,
            },
        )

        body = self.client.get(self._url()).json()
        self.assertIsNotNone(body["pending_dropoff"])
        self.assertEqual(body["pending_dropoff"]["asset_id"], cuba.id)
        self.assertEqual(cuba.current_transaction_status(), "PICKED_UP")

    def test_most_recent_pending_cuba_wins_when_several_on_truck(self):
        old_cuba = make_asset(self.company, self.asset_type, employee=self.employee, zone=self.obra, name="CUBA-OLD")
        new_cuba = make_asset(self.company, self.asset_type, employee=self.employee, zone=self.obra, name="CUBA-NEW")
        old_plan = make_planning(
            self.company, asset=old_cuba, assigned_to=self.employee, planning_type=PlanningType.PORTE,
            service_type="porte_material", origin=self.obra, destination=self.destino,
            status=PlanningStatus.NOT_COMPLETED, scheduled_date=date.today() - timedelta(days=5),
        )
        new_plan = make_planning(
            self.company, asset=new_cuba, assigned_to=self.employee, planning_type=PlanningType.CUBA,
            status=PlanningStatus.DELIVERED, scheduled_date=date.today(),
        )
        AssetTransaction.objects.create(
            company=self.company, asset=old_cuba, from_zone=self.obra,
            transaction_type=TransactionTypeChoices.PORTE_MOVEMENT,
            date=timezone.now() - timedelta(days=3),
            performed_by=self.employee, planning=old_plan,
        )
        AssetTransaction.objects.create(
            company=self.company, asset=new_cuba, from_zone=self.obra,
            transaction_type=TransactionTypeChoices.MOVEMENT,
            date=timezone.now(),
            performed_by=self.employee, planning=new_plan,
        )

        body = self.client.get(self._url()).json()
        self.assertIsNotNone(body["pending_dropoff"])
        self.assertEqual(body["pending_dropoff"]["asset_id"], new_cuba.id)
        self.assertEqual(body["pending_dropoff"]["planning_id"], new_plan.id)

    def _url(self):
        return "/en/api/plannings/next/"
