"""
assets/tests/test_permissions.py

Tests for the two permission boundaries added on top of asset planning:
  - Approving/rejecting an AssetPlanningRequest requires its own
    "assets.approve_assetplanningrequest" permission, separate from
    add/change (see AssetPlanningRequestAdmin.has_approve_permission).
  - The Schedule calendar is read-only (no drag/resize/unassign/duplicate/
    add, and no backlog) for anyone without "assets.change_assetplanning"
    (see AssetScheduleView.get_context_data's `can_edit`).

Run just this file:
    python manage.py test assets.tests.test_permissions
"""
from datetime import date, time, timedelta

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.contrib.messages.middleware import MessageMiddleware
from django.contrib.sessions.middleware import SessionMiddleware
from django.core.exceptions import PermissionDenied
from django.test import RequestFactory, TestCase
from django.urls import reverse
from django.utils import timezone

from assets.admin.asset_planning_request import AssetPlanningRequestAdmin
from assets.models import Asset, AssetPlanning, AssetPlanningRequest, AssetPlanningRequestStatus, AssetType, AssetTypeCategory
from management.models import ProjectFile
from sales.models import Client
from api.fleet.tests.helpers import make_asset_type, make_company, make_employee, make_user, make_zone

User = get_user_model()
TODAY = date(2026, 8, 10)
SCHEDULE_TODAY = timezone.localdate()


def _grant(user, *codenames):
    perms = Permission.objects.filter(codename__in=codenames, content_type__app_label="assets")
    assert perms.count() == len(codenames), f"expected {len(codenames)} perms, found {perms.count()}"
    user.user_permissions.add(*perms)


class ApprovePermissionTests(TestCase):
    def setUp(self):
        self.company = make_company()
        self.to_zone = make_zone(self.company, name="To Zone")
        self.asset = Asset.objects.create(company=self.company, asset_type=make_asset_type(self.company), name="Machine 1")
        client = Client.objects.create(name="Client 1", company=self.company)
        self.project_file = ProjectFile.objects.create(name="Project 1", client=client, company=self.company)

        self.request_obj = AssetPlanningRequest.objects.create(
            company=self.company, service_type="servicio_grua", to_zone=self.to_zone,
            needed_date=TODAY, asset=self.asset, project_file=self.project_file,
            estimated_duration=timedelta(hours=1),
        )
        self.admin = AssetPlanningRequestAdmin(AssetPlanningRequest, admin_site=None)

    def _request(self, user):
        req = RequestFactory().post("/")
        SessionMiddleware(lambda r: None).process_request(req)
        req.session.save()
        MessageMiddleware(lambda r: None).process_request(req)
        req.user = user
        return req

    def test_approve_without_permission_is_denied(self):
        user = User.objects.create_user(username="creator", password="x", is_staff=True)
        _grant(user, "add_assetplanningrequest", "view_assetplanningrequest")

        with self.assertRaises(PermissionDenied):
            self.admin.approve_submit_action(self._request(user), self.request_obj)

        self.request_obj.refresh_from_db()
        self.assertEqual(self.request_obj.status, AssetPlanningRequestStatus.PENDING_REVIEW)

    def test_reject_without_permission_is_denied(self):
        user = User.objects.create_user(username="creator2", password="x", is_staff=True)
        _grant(user, "add_assetplanningrequest", "view_assetplanningrequest")

        with self.assertRaises(PermissionDenied):
            self.admin.reject_submit_action(self._request(user), self.request_obj)

        self.request_obj.refresh_from_db()
        self.assertEqual(self.request_obj.status, AssetPlanningRequestStatus.PENDING_REVIEW)

    def test_approve_with_permission_succeeds(self):
        reviewer = User.objects.create_user(username="reviewer", password="x", is_staff=True)
        _grant(reviewer, "view_assetplanningrequest", "approve_assetplanningrequest")

        self.admin.approve_submit_action(self._request(reviewer), self.request_obj)

        self.request_obj.refresh_from_db()
        self.assertEqual(self.request_obj.status, AssetPlanningRequestStatus.APPROVED)
        self.assertIsNotNone(self.request_obj.resulting_planning)

    def test_approve_crane_without_asset_succeeds(self):
        self.request_obj.asset = None
        self.request_obj.save(update_fields=["asset"])
        reviewer = User.objects.create_user(username="reviewer2", password="x", is_staff=True)
        _grant(reviewer, "view_assetplanningrequest", "approve_assetplanningrequest")

        self.admin.approve_submit_action(self._request(reviewer), self.request_obj)

        self.request_obj.refresh_from_db()
        self.assertEqual(self.request_obj.status, AssetPlanningRequestStatus.APPROVED)
        self.assertIsNone(self.request_obj.resulting_planning.asset_id)

    def test_approve_own_machine_without_asset_is_blocked(self):
        self.request_obj.service_type = "porte_maquina"
        self.request_obj.asset = None
        self.request_obj.from_zone = self.to_zone
        self.request_obj.save(update_fields=["service_type", "asset", "from_zone"])
        reviewer = User.objects.create_user(username="reviewer3", password="x", is_staff=True)
        _grant(reviewer, "view_assetplanningrequest", "approve_assetplanningrequest")

        self.admin.approve_submit_action(self._request(reviewer), self.request_obj)

        self.request_obj.refresh_from_db()
        self.assertEqual(self.request_obj.status, AssetPlanningRequestStatus.PENDING_REVIEW)
        self.assertIsNone(self.request_obj.resulting_planning_id)

    def test_a_plain_creator_can_still_create_a_request(self):
        # Sanity check: add permission alone is genuinely enough to create one —
        # this is what "can create but not approve" means in practice.
        user = User.objects.create_user(username="creator3", password="x", is_staff=True)
        _grant(user, "add_assetplanningrequest", "view_assetplanningrequest")
        self.assertTrue(user.has_perm("assets.add_assetplanningrequest"))
        self.assertFalse(user.has_perm("assets.approve_assetplanningrequest"))


class ScheduleReadOnlyPermissionTests(TestCase):
    def setUp(self):
        self.company = make_company()
        self.origin = make_zone(self.company, name="Origin")
        self.destination = make_zone(self.company, name="Destination")
        self.cargo = Asset.objects.create(company=self.company, asset_type=make_asset_type(self.company), name="Cuba 1")

        self.viewer = User.objects.create_user(username="viewer", password="x", is_staff=True)
        _grant(self.viewer, "view_asset")
        make_employee(self.company, self.viewer)

        self.editor = User.objects.create_user(username="editor", password="x", is_staff=True)
        _grant(self.editor, "view_asset", "change_assetplanning")
        make_employee(self.company, self.editor)

        truck_type = AssetType.objects.create(company=self.company, name="CAMIONES", category=AssetTypeCategory.TRUCK)
        self.truck = Asset.objects.create(company=self.company, asset_type=truck_type, name="Truck 1")

        self.driver = make_employee(self.company, make_user(username="driver1"))
        self.planning = AssetPlanning.objects.create(
            company=self.company, asset=self.cargo, origin=self.origin, destination=self.destination,
            assigned_to=self.driver, transport_asset=self.truck,
            scheduled_date=SCHEDULE_TODAY, scheduled_time=time(8, 0), estimated_duration=timedelta(hours=1),
        )
        # A pending/unassigned one too, so the backlog would show something *if* allowed to.
        AssetPlanning.objects.create(
            company=self.company, asset=self.cargo, origin=self.origin, destination=self.destination,
            scheduled_date=SCHEDULE_TODAY,
        )
        self.url = reverse("admin:assets_asset_schedule")

    def test_viewer_sees_can_edit_false_and_no_backlog(self):
        self.client.force_login(self.viewer)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context["can_edit"])
        self.assertEqual(response.context["backlog_items"], [])
        self.assertEqual(response.context["backlog_count"], 0)
        # Blocks (already-scheduled, view-only data) still render.
        self.assertEqual(len(response.context["blocks"]), 1)

    def test_editor_sees_can_edit_true_and_backlog(self):
        self.client.force_login(self.editor)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["can_edit"])
        self.assertEqual(response.context["backlog_count"], 1)

    def test_viewer_cannot_move_a_block(self):
        self.client.force_login(self.viewer)
        response = self.client.post(
            self.url,
            data={"action": "move", "id": self.planning.pk, "start": 9 * 60, "end": 10 * 60,
                  "person_id": self.driver.pk, "day": TODAY.isoformat()},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 403)
        self.planning.refresh_from_db()
        self.assertEqual(self.planning.scheduled_time, time(8, 0))

    def test_viewer_cannot_duplicate(self):
        self.client.force_login(self.viewer)
        response = self.client.post(
            self.url,
            data={"action": "duplicate_next_day", "id": self.planning.pk},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(AssetPlanning.objects.filter(asset=self.cargo).count(), 2)
