"""
assets/tests/test_request_rereview.py

When an approved AssetPlanningRequest is edited, it returns to pending_review
while keeping resulting_planning. Reviewers must see awaits_rereview / schedule
request_changes_pending. Approve still creates a new plan (unchanged).

Run:
    python manage.py test assets.tests.test_request_rereview
"""
from datetime import date, time, timedelta

from django.contrib.auth import get_user_model
from django.contrib.messages.middleware import MessageMiddleware
from django.contrib.sessions.middleware import SessionMiddleware
from django.test import RequestFactory, TestCase
from django.urls import reverse
from django.utils import timezone

from assets.admin.asset_planning_request import AssetPlanningRequestAdmin
from assets.models import (
    Asset,
    AssetPlanning,
    AssetPlanningRequest,
    AssetPlanningRequestStatus,
    AssetType,
    AssetTypeCategory,
)
from assets.views.asset_schedule import _planning_card_data
from company.models import Employee
from management.models import ProjectFile
from sales.models import Client
from api.fleet.tests.helpers import make_asset_type, make_company, make_employee, make_zone

User = get_user_model()
NEEDED_DATE = date(2026, 8, 10)
SCHEDULE_TODAY = timezone.localdate()


class AwaitsRereviewTests(TestCase):
    def setUp(self):
        self.company = make_company()
        self.to_zone = make_zone(self.company, name="To Zone")
        self.asset = Asset.objects.create(
            company=self.company, asset_type=make_asset_type(self.company), name="Machine 1",
        )
        client = Client.objects.create(name="Client 1", company=self.company)
        self.project_file = ProjectFile.objects.create(name="Project 1", client=client, company=self.company)
        self.superuser = User.objects.create_superuser(username="admin", password="x", email="a@a.com")
        make_employee(self.company, self.superuser)

        self.request_obj = AssetPlanningRequest.objects.create(
            company=self.company,
            service_type="servicio_grua",
            to_zone=self.to_zone,
            needed_date=NEEDED_DATE,
            project_file=self.project_file,
            estimated_duration=timedelta(hours=1),
        )

    def _approve(self):
        req = RequestFactory().post("/")
        SessionMiddleware(lambda r: None).process_request(req)
        req.session.save()
        MessageMiddleware(lambda r: None).process_request(req)
        req.user = self.superuser
        AssetPlanningRequestAdmin(AssetPlanningRequest, admin_site=None).approve_submit_action(
            req, self.request_obj,
        )

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
        card = _planning_card_data(planning)
        self.assertTrue(card["is_old_plan"])
        self.assertIn("Old plan", card["old_plan_label"])

    def test_reapprove_marks_previous_planning_superseded(self):
        self._approve()
        self.request_obj.refresh_from_db()
        old = self.request_obj.resulting_planning

        self.request_obj.notes = "Changed again"
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
        card = _planning_card_data(old)
        self.assertTrue(card["is_old_plan"])
        self.assertTrue(card["is_superseded"])
        self.assertEqual(card["old_plan_label"], "Old plan")

    def test_planning_card_data_flags_request_changes(self):
        self._approve()
        self.request_obj.refresh_from_db()
        planning = self.request_obj.resulting_planning
        planning.assigned_to = make_employee(self.company, User.objects.create_user(username="d1", password="x"))
        truck_type = AssetType.objects.create(
            company=self.company, name="CAMIONES", category=AssetTypeCategory.TRUCK,
        )
        planning.transport_asset = Asset.objects.create(
            company=self.company, asset_type=truck_type, name="Truck 1",
        )
        planning.scheduled_date = SCHEDULE_TODAY
        planning.scheduled_time = time(8, 0)
        planning.estimated_duration = timedelta(hours=1)
        planning.save()

        self.request_obj.notes = "Changed"
        self.request_obj.save()

        planning = AssetPlanning.objects.select_related("source_request").get(pk=planning.pk)
        card = _planning_card_data(planning)
        self.assertTrue(card["request_changes_pending"])
        self.assertIn(
            reverse("admin:assets_assetplanningrequest_change", args=[self.request_obj.pk]),
            card["request_url"],
        )

    def test_schedule_block_includes_request_changes_pending(self):
        self._approve()
        self.request_obj.refresh_from_db()
        planning = self.request_obj.resulting_planning
        driver = Employee.objects.get(user=self.superuser)
        truck_type = AssetType.objects.create(
            company=self.company, name="CAMIONES", category=AssetTypeCategory.TRUCK,
        )
        truck = Asset.objects.create(company=self.company, asset_type=truck_type, name="Truck 1")
        planning.assigned_to = driver
        planning.transport_asset = truck
        planning.scheduled_date = SCHEDULE_TODAY
        planning.scheduled_time = time(8, 0)
        planning.estimated_duration = timedelta(hours=1)
        planning.save()

        self.request_obj.notes = "Reschedule please"
        self.request_obj.save()

        self.client.force_login(self.superuser)
        response = self.client.get(reverse("admin:assets_asset_schedule"))
        self.assertEqual(response.status_code, 200)
        blocks = response.context["blocks"]
        match = next((b for b in blocks if b["id"] == planning.pk), None)
        self.assertIsNotNone(match)
        self.assertTrue(match["request_changes_pending"])

    def test_superseded_old_plan_still_appears_on_schedule(self):
        self._approve()
        self.request_obj.refresh_from_db()
        old = self.request_obj.resulting_planning
        driver = Employee.objects.get(user=self.superuser)
        truck_type = AssetType.objects.create(
            company=self.company, name="CAMIONES", category=AssetTypeCategory.TRUCK,
        )
        truck = Asset.objects.create(company=self.company, asset_type=truck_type, name="Truck 1")
        old.assigned_to = driver
        old.transport_asset = truck
        old.scheduled_date = SCHEDULE_TODAY
        old.scheduled_time = time(8, 0)
        old.estimated_duration = timedelta(hours=1)
        old.save()

        self.request_obj.notes = "Replace me"
        self.request_obj.save()
        self._approve()
        old.refresh_from_db()
        self.assertEqual(old.status, "cancelled")
        self.assertTrue(old.is_superseded)

        self.client.force_login(self.superuser)
        response = self.client.get(reverse("admin:assets_asset_schedule"))
        self.assertEqual(response.status_code, 200)
        match = next((b for b in response.context["blocks"] if b["id"] == old.pk), None)
        self.assertIsNotNone(match)
        self.assertTrue(match["is_old_plan"])
        self.assertTrue(match["is_superseded"])
