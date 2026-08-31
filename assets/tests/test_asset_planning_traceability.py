"""
assets/tests/test_asset_planning_traceability.py

Tests for AssetPlanning's assignment traceability:
  - AssetPlanning.save() auto-tags assignment_origin (unassigned/manual/auto)
  - has_conflict / traceability_label
  - the "Assignment Status" changelist filter (TraceabilityFilter)

See assets/models.py (AssetPlanning.save/has_conflict/traceability_label)
and assets/admin/asset_planning.py (TraceabilityFilter).

Run just this file:
    python manage.py test assets.tests.test_asset_planning_traceability
"""
from datetime import datetime, time, timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext as _

from assets.models import Asset, AssetPlanning, AssetType, AssetTypeCategory, AssignmentOrigin, PlanningStatus
from api.fleet.tests.helpers import make_asset_type, make_company, make_employee, make_user, make_zone

User = get_user_model()
TODAY = timezone.localdate()
ONE_HOUR = timedelta(hours=1)


def make_truck(company):
    truck_type = AssetType.objects.create(company=company, name="CAMIONES", category=AssetTypeCategory.TRUCK)
    return Asset.objects.create(company=company, asset_type=truck_type, name="Truck 1")


def _duration(start, end):
    return datetime.combine(TODAY, end) - datetime.combine(TODAY, start)


class AssignmentOriginTaggingTests(TestCase):
    def setUp(self):
        self.company = make_company()
        self.origin = make_zone(self.company, name="Origin")
        self.destination = make_zone(self.company, name="Destination")
        self.cargo = Asset.objects.create(company=self.company, asset_type=make_asset_type(self.company, "Cuba"), name="Cuba 1")
        self.truck = make_truck(self.company)
        self.driver = make_employee(self.company, make_user(username="driver1"))

    def test_created_without_assignment_is_unassigned(self):
        planning = AssetPlanning.objects.create(
            company=self.company, asset=self.cargo, origin=self.origin, destination=self.destination,
            scheduled_date=TODAY,
        )
        self.assertEqual(planning.assignment_origin, AssignmentOrigin.UNASSIGNED)
        self.assertEqual(planning.traceability_label, _("Pending"))

    def test_created_with_assignment_is_manual(self):
        planning = AssetPlanning.objects.create(
            company=self.company, asset=self.cargo, origin=self.origin, destination=self.destination,
            scheduled_date=TODAY, scheduled_time=time(8, 0), estimated_duration=ONE_HOUR,
            assigned_to=self.driver, transport_asset=self.truck,
        )
        self.assertEqual(planning.assignment_origin, AssignmentOrigin.MANUAL)
        self.assertEqual(planning.traceability_label, _("Manual"))

    def test_editing_assignment_flips_to_manual(self):
        planning = AssetPlanning.objects.create(
            company=self.company, asset=self.cargo, origin=self.origin, destination=self.destination,
            scheduled_date=TODAY, scheduled_time=time(8, 0), estimated_duration=ONE_HOUR,
            assigned_to=self.driver, transport_asset=self.truck,
        )
        # Seed AUTO the way SimpleAssigner actually does it — via update_fields
        # — rather than via .create(), which save() always re-tags as MANUAL
        # (a freshly created row with both driver+truck already set is, by
        # definition, someone's manual choice at creation time).
        planning.assignment_origin = AssignmentOrigin.AUTO
        planning.save(update_fields=["assignment_origin"])
        self.assertEqual(planning.assignment_origin, AssignmentOrigin.AUTO)

        other_driver = make_employee(self.company, make_user(username="driver2"))
        planning.assigned_to = other_driver
        planning.save()
        planning.refresh_from_db()
        self.assertEqual(planning.assignment_origin, AssignmentOrigin.MANUAL)

    def test_clearing_assignment_resets_to_unassigned(self):
        planning = AssetPlanning.objects.create(
            company=self.company, asset=self.cargo, origin=self.origin, destination=self.destination,
            scheduled_date=TODAY, scheduled_time=time(8, 0), estimated_duration=ONE_HOUR,
            assigned_to=self.driver, transport_asset=self.truck,
        )
        planning.assigned_to = None
        planning.transport_asset = None
        planning.save()
        planning.refresh_from_db()
        self.assertEqual(planning.assignment_origin, AssignmentOrigin.UNASSIGNED)

    def test_explicit_update_fields_is_respected(self):
        """TransportAssigner sets assignment_origin=AUTO explicitly — save() must not override it."""
        planning = AssetPlanning.objects.create(
            company=self.company, asset=self.cargo, origin=self.origin, destination=self.destination,
            scheduled_date=TODAY,
        )
        planning.assigned_to = self.driver
        planning.transport_asset = self.truck
        planning.scheduled_time = time(8, 0)
        planning.estimated_duration = ONE_HOUR
        planning.assignment_origin = AssignmentOrigin.AUTO
        planning.save(update_fields=[
            "assigned_to", "transport_asset", "scheduled_time", "estimated_duration", "assignment_origin",
        ])
        planning.refresh_from_db()
        self.assertEqual(planning.assignment_origin, AssignmentOrigin.AUTO)
        self.assertEqual(planning.traceability_label, _("Auto-assigned"))

    def test_resize_only_save_does_not_touch_origin(self):
        planning = AssetPlanning.objects.create(
            company=self.company, asset=self.cargo, origin=self.origin, destination=self.destination,
            scheduled_date=TODAY, scheduled_time=time(8, 0), estimated_duration=ONE_HOUR,
            assigned_to=self.driver, transport_asset=self.truck,
        )
        AssetPlanning.objects.filter(pk=planning.pk).update(assignment_origin=AssignmentOrigin.AUTO)
        planning.refresh_from_db()
        planning.estimated_duration = timedelta(hours=2)
        planning.save(update_fields=["estimated_duration"])
        planning.refresh_from_db()
        self.assertEqual(planning.assignment_origin, AssignmentOrigin.AUTO)


class HasConflictTests(TestCase):
    def setUp(self):
        self.company = make_company()
        self.origin = make_zone(self.company, name="Origin")
        self.destination = make_zone(self.company, name="Destination")
        self.cargo = Asset.objects.create(company=self.company, asset_type=make_asset_type(self.company, "Cuba"), name="Cuba 1")
        self.truck = make_truck(self.company)
        self.driver = make_employee(self.company, make_user(username="driver1"))

    def _make(self, start, end, driver=None, truck=None):
        return AssetPlanning.objects.create(
            company=self.company, asset=self.cargo, origin=self.origin, destination=self.destination,
            scheduled_date=TODAY, scheduled_time=start, estimated_duration=_duration(start, end),
            assigned_to=driver or self.driver, transport_asset=truck or self.truck,
        )

    def test_no_conflict_when_alone(self):
        p = self._make(time(8, 0), time(9, 0))
        self.assertFalse(p.has_conflict)
        self.assertEqual(p.traceability_label, _("Manual"))

    def test_conflict_when_overlapping_same_driver(self):
        p1 = self._make(time(8, 0), time(9, 0))
        p2 = self._make(time(8, 30), time(9, 30))
        self.assertTrue(p1.has_conflict)
        self.assertTrue(p2.has_conflict)
        self.assertEqual(p1.traceability_label, _("Conflict"))

    def test_no_conflict_when_back_to_back(self):
        p1 = self._make(time(8, 0), time(9, 0))
        p2 = self._make(time(9, 0), time(10, 0))
        self.assertFalse(p1.has_conflict)
        self.assertFalse(p2.has_conflict)


class TraceabilityFilterTests(TestCase):
    def setUp(self):
        self.company = make_company()
        self.origin = make_zone(self.company, name="Origin")
        self.destination = make_zone(self.company, name="Destination")
        self.cargo = Asset.objects.create(company=self.company, asset_type=make_asset_type(self.company, "Cuba"), name="Cuba 1")
        self.truck = make_truck(self.company)
        self.driver = make_employee(self.company, make_user(username="driver1"))

        self.pending = AssetPlanning.objects.create(
            company=self.company, asset=self.cargo, origin=self.origin, destination=self.destination,
            scheduled_date=TODAY,
        )
        self.manual = AssetPlanning.objects.create(
            company=self.company, asset=self.cargo, origin=self.origin, destination=self.destination,
            scheduled_date=TODAY, scheduled_time=time(8, 0), estimated_duration=ONE_HOUR,
            assigned_to=self.driver, transport_asset=self.truck,
        )

        superuser = User.objects.create_superuser(username="admin", password="testpass123", email="a@a.com")
        make_employee(self.company, superuser)
        self.client.force_login(superuser)
        self.url = reverse("admin:assets_assetplanning_changelist")

    def test_pending_filter_shows_only_pending(self):
        response = self.client.get(self.url, {"traceability": "pending"})
        ids = {obj.pk for obj in response.context["cl"].queryset}
        self.assertEqual(ids, {self.pending.pk})

    def test_manual_filter_shows_only_manual(self):
        response = self.client.get(self.url, {"traceability": "manual"})
        ids = {obj.pk for obj in response.context["cl"].queryset}
        self.assertEqual(ids, {self.manual.pk})


class TraceabilityChangelistConflictTests(TestCase):
    """Covers the batched conflict path (attach_conflict_flags), not just the per-object one in HasConflictTests."""

    def setUp(self):
        self.company = make_company()
        self.origin = make_zone(self.company, name="Origin")
        self.destination = make_zone(self.company, name="Destination")
        self.cargo = Asset.objects.create(company=self.company, asset_type=make_asset_type(self.company, "Cuba"), name="Cuba 1")
        self.truck = make_truck(self.company)
        self.driver = make_employee(self.company, make_user(username="driver1"))

        def make(start, end, driver=None, truck=None):
            return AssetPlanning.objects.create(
                company=self.company, asset=self.cargo, origin=self.origin, destination=self.destination,
                scheduled_date=TODAY, scheduled_time=start, estimated_duration=_duration(start, end),
                assigned_to=driver or self.driver, transport_asset=truck or self.truck,
            )

        self.conflict_a = make(time(8, 0), time(9, 0))
        self.conflict_b = make(time(8, 30), time(9, 30))
        self.clean = make(time(11, 0), time(12, 0))
        self.pending = AssetPlanning.objects.create(
            company=self.company, asset=self.cargo, origin=self.origin, destination=self.destination,
            scheduled_date=TODAY,
        )

        superuser = User.objects.create_superuser(username="admin2", password="testpass123", email="a2@a.com")
        make_employee(self.company, superuser)
        self.client.force_login(superuser)
        self.url = reverse("admin:assets_assetplanning_changelist")

    def test_conflict_filter_shows_only_conflicting_via_batch_path(self):
        response = self.client.get(self.url, {"traceability": "conflict"})
        ids = {obj.pk for obj in response.context["cl"].queryset}
        self.assertEqual(ids, {self.conflict_a.pk, self.conflict_b.pk})

    def test_unfiltered_changelist_column_matches_per_object_has_conflict(self):
        response = self.client.get(self.url)
        result_list = {obj.pk: obj for obj in response.context["cl"].result_list}
        self.assertTrue(result_list[self.conflict_a.pk].has_conflict)
        self.assertTrue(result_list[self.conflict_b.pk].has_conflict)
        self.assertFalse(result_list[self.clean.pk].has_conflict)
        self.assertFalse(result_list[self.pending.pk].has_conflict)

        # Must match the unbatched per-object query.
        self.assertEqual(
            result_list[self.conflict_a.pk].has_conflict,
            AssetPlanning.objects.get(pk=self.conflict_a.pk).has_conflict,
        )
        self.assertEqual(
            result_list[self.clean.pk].has_conflict,
            AssetPlanning.objects.get(pk=self.clean.pk).has_conflict,
        )


class DriverClaimedCubaInvariantTests(TestCase):
    def setUp(self):
        self.company = make_company()
        self.origin = make_zone(self.company, name="Origin")
        self.destination = make_zone(self.company, name="Destination")
        self.cargo = Asset.objects.create(
            company=self.company, asset_type=make_asset_type(self.company, "Cuba"), name="Cuba 1",
        )

    def test_clearing_asset_clears_driver_claimed_cuba(self):
        planning = AssetPlanning.objects.create(
            company=self.company, asset=self.cargo, origin=self.origin, destination=self.destination,
            scheduled_date=TODAY, status=PlanningStatus.DELIVERED, driver_claimed_cuba=True,
        )
        planning.asset = None
        planning.status = PlanningStatus.PENDING
        planning.save(update_fields=["asset", "status"])
        planning.refresh_from_db()
        self.assertFalse(planning.driver_claimed_cuba)
