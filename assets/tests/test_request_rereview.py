"""
assets/tests/test_request_rereview.py

When an approved AssetPlanningRequest is edited, it returns to pending_review
while keeping resulting_planning. Reviewers must see awaits_rereview / schedule
request_changes_pending. Approve still creates a new plan (unchanged).

Run:
    python manage.py test assets.tests.test_request_rereview
"""
from datetime import date, timedelta

from django.test import TestCase
from django.urls import reverse
from django.utils import translation

from assets.models import AssetPlanningRequest, AssetPlanningRequestStatus
from assets.tests.factories import PlanningRequestFixtureMixin, approve_request, make_truck, schedule_planning
from assets.views.asset_schedule import _planning_card_data

NEEDED_DATE = date(2026, 8, 10)


class AwaitsRereviewTests(PlanningRequestFixtureMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.request_obj = AssetPlanningRequest.objects.create(
            company=self.company,
            service_type="servicio_grua",
            to_zone=self.to_zone,
            needed_date=NEEDED_DATE,
            project_file=self.project_file,
            estimated_duration=timedelta(hours=1),
        )

    def _approve(self):
        approve_request(self.request_obj, self.superuser)

    def test_fresh_pending_request_does_not_await_rereview(self):
        self.assertFalse(self.request_obj.awaits_rereview)

    def test_approved_request_does_not_await_rereview(self):
        self._approve()
        self.request_obj.refresh_from_db()
        self.assertEqual(self.request_obj.status, AssetPlanningRequestStatus.APPROVED)
        self.assertFalse(self.request_obj.awaits_rereview)
        self.assertFalse(self.request_obj.resulting_planning.has_pending_request_changes)

    def test_edit_after_approve_sets_awaits_rereview(self):
        self._approve()
        self.request_obj.refresh_from_db()
        planning = self.request_obj.resulting_planning

        self.request_obj.notes = "Updated after approval"
        self.request_obj.save()
        self.request_obj.refresh_from_db()

        self.assertEqual(self.request_obj.status, AssetPlanningRequestStatus.PENDING_REVIEW)
        self.assertTrue(self.request_obj.awaits_rereview)
        self.assertEqual(self.request_obj.resulting_planning_id, planning.pk)
        planning.refresh_from_db()
        self.assertTrue(planning.has_pending_request_changes)
        self.assertTrue(planning.is_old_plan)
        # _planning_card_data() translates old_plan_label at call time using
        # whatever language is currently active. Outside a request (no
        # LocaleMiddleware involved), that defaults to LANGUAGE_CODE ("es"),
        # so force English here to assert against the source string.
        with translation.override("en"):
            card = _planning_card_data(planning)
        self.assertTrue(card["is_old_plan"])
        self.assertIn("Old plan", card["old_plan_label"])

    def test_schedule_block_includes_request_changes_pending(self):
        self._approve()
        self.request_obj.refresh_from_db()
        planning = self.request_obj.resulting_planning
        schedule_planning(planning, driver=self.superuser_employee, truck=make_truck(self.company))

        self.request_obj.notes = "Reschedule please"
        self.request_obj.save()

        self.client.force_login(self.superuser)
        response = self.client.get(reverse("admin:assets_asset_schedule"))
        self.assertEqual(response.status_code, 200)
        blocks = response.context["blocks"]
        match = next((b for b in blocks if b["id"] == planning.pk), None)
        self.assertIsNotNone(match)
        self.assertTrue(match["request_changes_pending"])
        self.assertIn(
            reverse("admin:assets_assetplanningrequest_change", args=[self.request_obj.pk]),
            match["request_url"],
        )

    def test_superseded_old_plan_still_appears_on_schedule(self):
        self._approve()
        self.request_obj.refresh_from_db()
        old = self.request_obj.resulting_planning
        schedule_planning(old, driver=self.superuser_employee, truck=make_truck(self.company))

        self.request_obj.notes = "Replace me"
        self.request_obj.save()
        self.request_obj.refresh_from_db()
        self.assertTrue(self.request_obj.awaits_rereview)

        self._approve()
        self.request_obj.refresh_from_db()
        new = self.request_obj.resulting_planning
        old.refresh_from_db()
        self.assertNotEqual(old.pk, new.pk)
        self.assertEqual(old.superseded_by_id, new.pk)
        self.assertEqual(old.status, "cancelled")
        self.assertTrue(old.is_superseded)
        self.assertTrue(old.is_old_plan)
        self.assertFalse(new.is_old_plan)
        # Label is translated at call time; the view runs under the URL's
        # language, so pin English on the card builder directly.
        with translation.override("en"):
            card = _planning_card_data(old)
        self.assertTrue(card["is_old_plan"])
        self.assertTrue(card["is_superseded"])
        self.assertEqual(card["old_plan_label"], "Old plan")

        self.client.force_login(self.superuser)
        response = self.client.get(reverse("admin:assets_asset_schedule"))
        self.assertEqual(response.status_code, 200)
        match = next((b for b in response.context["blocks"] if b["id"] == old.pk), None)
        self.assertIsNotNone(match)
        self.assertTrue(match["is_old_plan"])
        self.assertTrue(match["is_superseded"])
