"""
api/fleet/tests/test_asset_assign.py

Tests for POST /en/api/assets/<id>/assign/ — the driver "self-assign" endpoint
a scan of a cuba's QR calls. Whatever asset gets scanned is pinned to the
scanning driver's own pending cuba plan for today — no check on whether that
asset already had some other plan; dispatch data isn't trusted enough to let
it block a driver mid-pickup.
"""
from datetime import date, timedelta

from django.utils import timezone
from rest_framework.test import APIClient, APITestCase

from api.fleet.tests.helpers import (
    make_asset,
    make_asset_type,
    make_company,
    make_employee,
    make_planning,
    make_user,
    make_zone,
)
from assets.models import AssetTransaction, CubaOperationType, PlanningStatus, PlanningType, TransactionTypeChoices
from assets.constants import AssignRequestKey


class AssetAssignViewTests(APITestCase):
    def setUp(self):
        self.client = APIClient()
        self.company = make_company()
        self.user = make_user()
        self.employee = make_employee(self.company, self.user)
        self.asset_type = make_asset_type(self.company)
        self.asset = make_asset(self.company, self.asset_type)

    def test_requires_authentication(self):
        response = self.client.post(f"/en/api/assets/{self.asset.id}/assign/")
        self.assertEqual(response.status_code, 401)

    def test_assigns_scanned_asset_to_my_own_pending_planning(self):
        self.client.force_authenticate(user=self.user)
        planning = make_planning(self.company, asset=None, assigned_to=self.employee, status=PlanningStatus.PENDING)

        response = self.client.post(f"/en/api/assets/{self.asset.id}/assign/")

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data["success"])
        self.assertEqual(data["planning_id"], planning.id)
        self.asset.refresh_from_db()
        planning.refresh_from_db()
        self.assertEqual(self.asset.assigned_to, self.employee)
        self.assertEqual(planning.asset, self.asset)

    def test_overrides_a_different_asset_already_pinned_to_my_planning(self):
        self.client.force_authenticate(user=self.user)
        other_asset = make_asset(self.company, self.asset_type)
        planning = make_planning(
            self.company, asset=other_asset, assigned_to=self.employee, status=PlanningStatus.PENDING,
        )

        response = self.client.post(f"/en/api/assets/{self.asset.id}/assign/")

        self.assertEqual(response.status_code, 200)
        planning.refresh_from_db()
        self.assertEqual(planning.asset, self.asset)

    def test_does_not_touch_another_drivers_planning(self):
        self.client.force_authenticate(user=self.user)
        other_user = make_user(username="other_driver")
        other_employee = make_employee(self.company, other_user)
        other_planning = make_planning(
            self.company, asset=self.asset, assigned_to=other_employee, status=PlanningStatus.PENDING,
        )

        response = self.client.post(f"/en/api/assets/{self.asset.id}/assign/")

        self.assertEqual(response.status_code, 400)
        other_planning.refresh_from_db()
        self.assertEqual(other_planning.assigned_to, other_employee)
        self.assertEqual(other_planning.asset, self.asset)

    def test_rejects_when_no_planning_exists_for_the_cuba_today(self):
        self.client.force_authenticate(user=self.user)
        response = self.client.post(f"/en/api/assets/{self.asset.id}/assign/")
        self.assertEqual(response.status_code, 400)
        self.asset.refresh_from_db()
        self.assertIsNone(self.asset.assigned_to)

    def test_ignores_a_planning_scheduled_for_another_day(self):
        self.client.force_authenticate(user=self.user)
        make_planning(
            self.company, asset=None, assigned_to=self.employee, status=PlanningStatus.PENDING,
            scheduled_date=date.today() + timedelta(days=1),
        )
        response = self.client.post(f"/en/api/assets/{self.asset.id}/assign/")
        self.assertEqual(response.status_code, 400)

    def test_assigns_scanned_asset_to_a_pending_porte_too(self):
        """The Home 'usar esta cuba' shortcut and a plain QR scan both need this
        to work for a porte's recogida, not just cuba."""
        self.client.force_authenticate(user=self.user)
        planning = make_planning(
            self.company, asset=None, assigned_to=self.employee, status=PlanningStatus.PENDING,
            planning_type=PlanningType.PORTE,
        )

        response = self.client.post(f"/en/api/assets/{self.asset.id}/assign/")

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["planning_id"], planning.id)
        planning.refresh_from_db()
        self.assertEqual(planning.asset, self.asset)

    def test_rejects_asset_from_another_company(self):
        other_company = make_company(name="Other Co")
        other_user = make_user(username="other_company_user")
        make_employee(other_company, other_user)
        self.client.force_authenticate(user=self.user)
        other_asset_type = make_asset_type(other_company)
        other_asset = make_asset(other_company, other_asset_type)

        response = self.client.post(f"/en/api/assets/{other_asset.id}/assign/")
        self.assertEqual(response.status_code, 403)

    def test_rejects_scanning_a_different_cuba_while_one_is_pending(self):
        self.client.force_authenticate(user=self.user)
        obra = make_zone(self.company, name="Obra")
        held = make_asset(self.company, self.asset_type, employee=self.employee, zone=obra, name="HELD")
        other = make_asset(self.company, self.asset_type, name="OTHER")
        held_plan = make_planning(
            self.company, asset=held, assigned_to=self.employee, status=PlanningStatus.DELIVERED,
        )
        make_planning(self.company, asset=None, assigned_to=self.employee, status=PlanningStatus.PENDING)
        AssetTransaction.objects.create(
            company=self.company, asset=held, from_zone=obra,
            transaction_type=TransactionTypeChoices.MOVEMENT, date=timezone.now(),
            performed_by=self.employee, planning=held_plan, data={"vasAVerter": ""},
        )

        response = self.client.post(f"/en/api/assets/{other.id}/assign/")
        self.assertEqual(response.status_code, 400)
        self.assertIn("cuba pendiente", response.json()["error"])

    def test_allows_scanning_another_cuba_when_pending_has_no_planning(self):
        """Home omits pending_dropoff without planning_id — assign must match,
        otherwise drivers are blocked with no card to resolve the leftover."""
        self.client.force_authenticate(user=self.user)
        obra = make_zone(self.company, name="Obra")
        held = make_asset(self.company, self.asset_type, employee=self.employee, zone=obra, name="HELD")
        other = make_asset(self.company, self.asset_type, name="OTHER")
        next_plan = make_planning(
            self.company, asset=None, assigned_to=self.employee, status=PlanningStatus.PENDING,
        )
        AssetTransaction.objects.create(
            company=self.company, asset=held, from_zone=obra,
            transaction_type=TransactionTypeChoices.MOVEMENT, date=timezone.now(),
            performed_by=self.employee, planning=None, data={"vasAVerter": ""},
        )

        response = self.client.post(f"/en/api/assets/{other.id}/assign/")
        self.assertEqual(response.status_code, 200)
        next_plan.refresh_from_db()
        self.assertEqual(next_plan.asset_id, other.id)

    def test_use_pending_cuba_is_rejected_while_verteo_is_owed(self):
        self.client.force_authenticate(user=self.user)
        obra = make_zone(self.company, name="Obra")
        held = make_asset(self.company, self.asset_type, employee=self.employee, zone=obra, name="HELD")
        held_plan = make_planning(
            self.company, asset=held, assigned_to=self.employee, status=PlanningStatus.DELIVERED,
        )
        next_plan = make_planning(
            self.company, asset=None, assigned_to=self.employee, status=PlanningStatus.PENDING,
        )
        AssetTransaction.objects.create(
            company=self.company, asset=held, from_zone=obra,
            transaction_type=TransactionTypeChoices.MOVEMENT, date=timezone.now(),
            performed_by=self.employee, planning=held_plan, data={"vasAVerter": "Sí"},
        )

        response = self.client.post(
            f"/en/api/assets/{held.id}/assign/",
            data={AssignRequestKey.USE_PENDING_CUBA: True},
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        next_plan.refresh_from_db()
        self.assertFalse(next_plan.driver_claimed_cuba)

    def test_use_pending_cuba_is_rejected_for_cambio_misma_cuba(self):
        self.client.force_authenticate(user=self.user)
        obra = make_zone(self.company, name="Obra")
        held = make_asset(self.company, self.asset_type, employee=self.employee, zone=obra, name="HELD")
        held_plan = make_planning(
            self.company, asset=held, assigned_to=self.employee, status=PlanningStatus.DELIVERED,
        )
        make_planning(
            self.company, asset=None, assigned_to=self.employee, status=PlanningStatus.PENDING,
            cuba_operation_type=CubaOperationType.CAMBIO_MISMA_CUBA,
        )
        AssetTransaction.objects.create(
            company=self.company, asset=held, from_zone=obra,
            transaction_type=TransactionTypeChoices.MOVEMENT, date=timezone.now(),
            performed_by=self.employee, planning=held_plan, data={"vasAVerter": ""},
        )

        response = self.client.post(
            f"/en/api/assets/{held.id}/assign/",
            data={AssignRequestKey.USE_PENDING_CUBA: True},
            format="json",
        )
        self.assertEqual(response.status_code, 400)

    def test_scanning_a_different_cuba_releases_the_reservation(self):
        self.client.force_authenticate(user=self.user)
        reserved = make_asset(self.company, self.asset_type, name="RESERVED")
        planning = make_planning(
            self.company, asset=None, assigned_to=self.employee, status=PlanningStatus.PENDING,
            reserved_asset=reserved,
        )

        response = self.client.post(f"/en/api/assets/{self.asset.id}/assign/")

        self.assertEqual(response.status_code, 200)
        planning.refresh_from_db()
        self.assertEqual(planning.asset, self.asset)
        self.assertIsNone(planning.reserved_asset_id)

    def test_scanning_the_reserved_cuba_keeps_the_reservation(self):
        self.client.force_authenticate(user=self.user)
        reserved = make_asset(self.company, self.asset_type, name="RESERVED")
        planning = make_planning(
            self.company, asset=None, assigned_to=self.employee, status=PlanningStatus.PENDING,
            reserved_asset=reserved,
        )

        response = self.client.post(f"/en/api/assets/{reserved.id}/assign/")

        self.assertEqual(response.status_code, 200)
        planning.refresh_from_db()
        self.assertEqual(planning.asset, reserved)
        self.assertEqual(planning.reserved_asset_id, reserved.id)
