"""
assets/tests/test_transport_assigner.py

Tests for TransportAssigner (v2 assignment: truck first, driver derived from
the truck's own link — Asset.assigned_to, falling back to MachinePermission-
authorized employees). See assets/services/transport_assigner.py.

Run just this file:
    python manage.py test assets.tests.test_transport_assigner
"""
from datetime import date, time, timedelta
from unittest.mock import patch

from django.test import TestCase

from assets.models import (
    Asset,
    AssetPlanning,
    MachinePermission,
    PlanningUrgency,
    TransportCompatibilityRule,
)
from assets.services.transport_assigner import TransportAssigner
from assets.tests.factories import make_truck, make_truck_type
from api.fleet.tests.helpers import make_asset_type, make_company, make_employee, make_user, make_zone

TODAY = date(2026, 8, 10)


def make_driver(company, username):
    return make_employee(company, make_user(username=username))


def _duration_between(start, end):
    if not start or not end:
        return None
    return timedelta(minutes=(end.hour * 60 + end.minute) - (start.hour * 60 + start.minute))


def make_planning(company, origin, destination, asset=None, urgency=PlanningUrgency.MEDIUM,
                   scheduled_time=None, scheduled_end_time=None, assigned_to=None, transport_asset=None,
                   time_locked=False, skip_auto_assignment=False, status="pending"):
    return AssetPlanning.objects.create(
        company=company,
        asset=asset or Asset.objects.create(company=company, asset_type=make_asset_type(company, "Cuba"), name="Cuba 1"),
        origin=origin,
        destination=destination,
        scheduled_date=TODAY,
        scheduled_time=scheduled_time,
        estimated_duration=_duration_between(scheduled_time, scheduled_end_time),
        urgency=urgency,
        assigned_to=assigned_to,
        transport_asset=transport_asset,
        time_locked=time_locked,
        skip_auto_assignment=skip_auto_assignment,
        status=status,
    )


# All tests patch route duration to a fixed 1h so they don't depend on
# GOOGLE_MAPS_API_KEY / network access.
@patch("assets.services.transport_assigner.GoogleRouteOptimizer.estimate_duration", return_value=timedelta(hours=1))
class TransportAssignerTests(TestCase):
    def setUp(self):
        self.company = make_company()
        self.origin = make_zone(self.company, name="Origin")
        self.destination = make_zone(self.company, name="Destination")
        self.truck_type = make_truck_type(self.company)
        self.driver = make_driver(self.company, "driver1")
        self.truck = make_truck(self.company, asset_type=self.truck_type, name="Truck 1", driver=self.driver)

    # ── Basic resolution ──────────────────────────────────────────────────
    def test_assigns_truck_and_derives_driver_from_its_own_link(self, _mock):
        planning = make_planning(self.company, self.origin, self.destination)

        result = TransportAssigner().assign_bulk([TODAY], self.company.id)

        self.assertEqual(result.assigned, [planning])
        planning.refresh_from_db()
        self.assertEqual(planning.transport_asset, self.truck)
        self.assertEqual(planning.assigned_to, self.driver)
        self.assertEqual(planning.scheduled_time, time(7, 0))
        self.assertEqual(planning.scheduled_end_time, time(8, 0))

    def test_skips_when_primary_driver_inactive_and_no_authorized_alternate(self, _mock):
        self.driver.is_active = False
        self.driver.save()
        planning = make_planning(self.company, self.origin, self.destination)

        result = TransportAssigner().assign_bulk([TODAY], self.company.id)

        self.assertEqual(result.assigned, [])
        self.assertEqual(result.skipped, [planning])
        planning.refresh_from_db()
        self.assertIsNone(planning.transport_asset)
        self.assertIsNone(planning.assigned_to)

    # ── Driver fallback via MachinePermission ───────────────────────────
    def test_falls_back_to_authorized_employee_when_primary_is_fully_booked(self, _mock):
        # Block the primary driver's entire working day — on a different
        # truck, so self.truck itself stays free; only the driver is busy.
        blocker_truck = make_truck(self.company, asset_type=self.truck_type, name="Blocker")
        make_planning(
            self.company, self.origin, self.destination,
            scheduled_time=time(7, 0), scheduled_end_time=time(17, 0),
            assigned_to=self.driver, transport_asset=blocker_truck,
        )
        alternate = make_driver(self.company, "alt1")
        MachinePermission.objects.create(company=self.company, employee=alternate, asset=self.truck)
        planning = make_planning(self.company, self.origin, self.destination)

        result = TransportAssigner().assign_bulk([TODAY], self.company.id)

        self.assertEqual(result.assigned, [planning])
        planning.refresh_from_db()
        self.assertEqual(planning.transport_asset, self.truck)
        self.assertEqual(planning.assigned_to, alternate)

    def test_picks_the_least_busy_authorized_alternate(self, _mock):
        blocker_truck = make_truck(self.company, asset_type=self.truck_type, name="Blocker")
        make_planning(
            self.company, self.origin, self.destination,
            scheduled_time=time(7, 0), scheduled_end_time=time(17, 0),
            assigned_to=self.driver, transport_asset=blocker_truck,
        )
        busy_alt = make_driver(self.company, "busy_alt")
        free_alt = make_driver(self.company, "free_alt")
        MachinePermission.objects.create(company=self.company, employee=busy_alt, asset=self.truck)
        MachinePermission.objects.create(company=self.company, employee=free_alt, asset=self.truck)
        # busy_alt already has two unrelated jobs that day (different truck/zone, just workload).
        other_truck = make_truck(self.company, asset_type=self.truck_type, name="Truck 2")
        for i in range(2):
            make_planning(
                self.company, self.origin, self.destination,
                scheduled_time=time(9 + i, 0), scheduled_end_time=time(10 + i, 0),
                assigned_to=busy_alt, transport_asset=other_truck,
            )
        planning = make_planning(self.company, self.origin, self.destination)

        result = TransportAssigner().assign_bulk([TODAY], self.company.id)

        self.assertEqual(result.assigned, [planning])
        planning.refresh_from_db()
        self.assertEqual(planning.assigned_to, free_alt)

    # ── Whichever side is already fixed narrows the other ───────────────
    def test_derives_truck_from_a_pre_set_driver(self, _mock):
        planning = make_planning(self.company, self.origin, self.destination, assigned_to=self.driver)

        result = TransportAssigner().assign_bulk([TODAY], self.company.id)

        self.assertEqual(result.assigned, [planning])
        planning.refresh_from_db()
        self.assertEqual(planning.transport_asset, self.truck)
        self.assertEqual(planning.assigned_to, self.driver)

    def test_derives_driver_from_a_pre_set_truck(self, _mock):
        planning = make_planning(self.company, self.origin, self.destination, transport_asset=self.truck)

        result = TransportAssigner().assign_bulk([TODAY], self.company.id)

        self.assertEqual(result.assigned, [planning])
        planning.refresh_from_db()
        self.assertEqual(planning.assigned_to, self.driver)

    def test_never_overrides_a_fully_manual_assignment_only_fills_time(self, _mock):
        other_truck = make_truck(self.company, asset_type=self.truck_type, name="Truck 0")  # sorts first by name
        other_driver = make_driver(self.company, "other")
        planning = make_planning(
            self.company, self.origin, self.destination,
            transport_asset=other_truck, assigned_to=other_driver,
        )

        result = TransportAssigner().assign_bulk([TODAY], self.company.id)

        self.assertEqual(result.assigned, [planning])
        planning.refresh_from_db()
        self.assertEqual(planning.transport_asset, other_truck)
        self.assertEqual(planning.assigned_to, other_driver)
        self.assertEqual(planning.scheduled_time, time(7, 0))

    # ── Compatibility (rules + manual exclusion) ────────────────────────
    def test_skips_incompatible_truck_in_favor_of_a_compatible_one(self, _mock):
        cuba = Asset.objects.create(company=self.company, asset_type=make_asset_type(self.company, "Cuba"), name="Cuba 1")
        cuba.data = {"peso_kg": 5000}
        cuba.save(update_fields=["data"])
        TransportCompatibilityRule.objects.create(
            company=self.company, name="Peso", condition_expression="truck.carga_maxima_kg >= cuba.peso_kg",
        )
        self.truck.data = {"carga_maxima_kg": 1000}  # too weak, sorts first by name
        self.truck.save(update_fields=["data"])
        strong_truck = make_truck(self.company, asset_type=self.truck_type, name="Truck 2", driver=self.driver)
        strong_truck.data = {"carga_maxima_kg": 9000}
        strong_truck.save(update_fields=["data"])
        planning = make_planning(self.company, self.origin, self.destination, asset=cuba)

        result = TransportAssigner().assign_bulk([TODAY], self.company.id)

        self.assertEqual(result.assigned, [planning])
        planning.refresh_from_db()
        self.assertEqual(planning.transport_asset, strong_truck)

    def test_manually_excluded_truck_is_never_picked(self, _mock):
        cuba = Asset.objects.create(company=self.company, asset_type=make_asset_type(self.company, "Cuba"), name="Cuba 1")
        other_truck = make_truck(self.company, asset_type=self.truck_type, name="Truck 2", driver=self.driver)
        cuba.excluded_assets.add(self.truck)  # only self.truck is excluded — "Truck 1" would sort first
        planning = make_planning(self.company, self.origin, self.destination, asset=cuba)

        result = TransportAssigner().assign_bulk([TODAY], self.company.id)

        self.assertEqual(result.assigned, [planning])
        planning.refresh_from_db()
        self.assertEqual(planning.transport_asset, other_truck)

    # ── time_locked ───────────────────────────────────────────────────────
    def test_time_locked_skips_instead_of_shifting_on_conflict(self, _mock):
        make_planning(
            self.company, self.origin, self.destination,
            scheduled_time=time(9, 0), scheduled_end_time=time(10, 0),
            assigned_to=self.driver, transport_asset=self.truck,
        )
        planning = make_planning(
            self.company, self.origin, self.destination,
            scheduled_time=time(9, 0), scheduled_end_time=time(10, 0),
            time_locked=True,
        )

        result = TransportAssigner().assign_bulk([TODAY], self.company.id)

        self.assertEqual(result.assigned, [])
        self.assertEqual(result.skipped, [planning])
        planning.refresh_from_db()
        self.assertEqual(planning.scheduled_time, time(9, 0))  # untouched
        self.assertIsNone(planning.transport_asset)

    def test_time_locked_succeeds_when_no_conflict(self, _mock):
        planning = make_planning(
            self.company, self.origin, self.destination,
            scheduled_time=time(9, 0), scheduled_end_time=time(10, 0), time_locked=True,
        )

        result = TransportAssigner().assign_bulk([TODAY], self.company.id)

        self.assertEqual(result.assigned, [planning])
        planning.refresh_from_db()
        self.assertEqual(planning.scheduled_time, time(9, 0))
        self.assertEqual(planning.scheduled_end_time, time(10, 0))

    def test_without_time_locked_shifts_to_another_slot_the_same_day(self, _mock):
        make_planning(
            self.company, self.origin, self.destination,
            scheduled_time=time(9, 0), scheduled_end_time=time(10, 0),
            assigned_to=self.driver, transport_asset=self.truck,
        )
        planning = make_planning(
            self.company, self.origin, self.destination,
            scheduled_time=time(9, 0), scheduled_end_time=time(10, 0),
        )

        result = TransportAssigner().assign_bulk([TODAY], self.company.id)

        self.assertEqual(result.assigned, [planning])
        planning.refresh_from_db()
        self.assertEqual(planning.scheduled_date, TODAY)  # day never moves
        self.assertNotEqual(planning.scheduled_time, time(9, 0))  # hour did move
        self.assertEqual(
            (planning.scheduled_end_time.hour * 60 + planning.scheduled_end_time.minute)
            - (planning.scheduled_time.hour * 60 + planning.scheduled_time.minute),
            60,  # duration preserved
        )

    # ── skip_auto_assignment ─────────────────────────────────────────────
    def test_skip_auto_assignment_is_never_touched_by_assign_one(self, _mock):
        planning = make_planning(self.company, self.origin, self.destination, skip_auto_assignment=True)

        assigned = TransportAssigner().assign_one(planning)

        self.assertFalse(assigned)
        planning.refresh_from_db()
        self.assertIsNone(planning.transport_asset)

    def test_skip_auto_assignment_is_excluded_from_bulk_run(self, _mock):
        planning = make_planning(self.company, self.origin, self.destination, skip_auto_assignment=True)

        result = TransportAssigner().assign_bulk([TODAY], self.company.id)

        self.assertNotIn(planning, result.assigned)
        self.assertNotIn(planning, result.skipped)
        planning.refresh_from_db()
        self.assertIsNone(planning.transport_asset)

    # ── Bulk scoping ──────────────────────────────────────────────────────
    def test_bulk_only_processes_pending_status(self, _mock):
        in_transit = make_planning(self.company, self.origin, self.destination, status="in_transit")
        cancelled = make_planning(self.company, self.origin, self.destination, status="cancelled")

        result = TransportAssigner().assign_bulk([TODAY], self.company.id)

        self.assertNotIn(in_transit, result.assigned + result.skipped)
        self.assertNotIn(cancelled, result.assigned + result.skipped)

    def test_default_bulk_never_touches_an_already_fully_assigned_planning(self, _mock):
        other_truck = make_truck(self.company, asset_type=self.truck_type, name="Truck 0", driver=self.driver)
        planning = make_planning(
            self.company, self.origin, self.destination,
            scheduled_time=time(9, 0), scheduled_end_time=time(10, 0),
            transport_asset=other_truck, assigned_to=self.driver,
        )

        result = TransportAssigner().assign_bulk([TODAY], self.company.id, reassign_all=False)

        self.assertNotIn(planning, result.assigned)
        self.assertNotIn(planning, result.skipped)
        planning.refresh_from_db()
        self.assertEqual(planning.transport_asset, other_truck)

    def test_reassign_all_resets_truck_and_driver_but_never_the_schedule(self, _mock):
        # The planning starts on a truck/driver pair that a from-scratch
        # resolution would NOT pick: "Truck 2" sorts after self.truck ("Truck 1"),
        # whose own driver is self.driver. Landing on self.truck/self.driver
        # proves the old assignment was reset, not just re-confirmed.
        other_driver = make_driver(self.company, "driver2")
        other_truck = make_truck(self.company, asset_type=self.truck_type, name="Truck 2", driver=other_driver)
        planning = make_planning(
            self.company, self.origin, self.destination,
            scheduled_time=time(9, 0), scheduled_end_time=time(12, 0),  # 3 hours — route mock says 1h
            transport_asset=other_truck, assigned_to=other_driver,
        )

        result = TransportAssigner().assign_bulk([TODAY], self.company.id, reassign_all=True)

        self.assertEqual(result.assigned, [planning])
        planning.refresh_from_db()
        self.assertEqual(planning.transport_asset, self.truck)
        self.assertEqual(planning.assigned_to, self.driver)
        # Duration is never recalculated once specified, and the slot is kept.
        self.assertEqual(planning.scheduled_time, time(9, 0))
        self.assertEqual(planning.scheduled_end_time, time(12, 0))

    # ── assign_one respects everything else ─────────────────────────────
    def test_assign_one_never_touches_other_plannings(self, _mock):
        other = make_planning(
            self.company, self.origin, self.destination,
            scheduled_time=time(9, 0), scheduled_end_time=time(10, 0),
            assigned_to=self.driver, transport_asset=self.truck,
        )
        planning = make_planning(self.company, self.origin, self.destination)

        assigned = TransportAssigner().assign_one(planning)

        self.assertTrue(assigned)
        other.refresh_from_db()
        self.assertEqual(other.scheduled_time, time(9, 0))
        self.assertEqual(other.assigned_to, self.driver)

    # ── Priority ordering ─────────────────────────────────────────────────
    def test_urgency_priority_wins_a_scarce_locked_slot(self, _mock):
        low = make_planning(
            self.company, self.origin, self.destination, urgency=PlanningUrgency.LOW,
            scheduled_time=time(9, 0), scheduled_end_time=time(10, 0), time_locked=True,
        )
        urgent = make_planning(
            self.company, self.origin, self.destination, urgency=PlanningUrgency.URGENT,
            scheduled_time=time(9, 0), scheduled_end_time=time(10, 0), time_locked=True,
        )

        result = TransportAssigner().assign_bulk([TODAY], self.company.id)

        urgent.refresh_from_db()
        low.refresh_from_db()
        self.assertIn(urgent, result.assigned)
        self.assertEqual(urgent.assigned_to, self.driver)
        self.assertIn(low, result.skipped)
        self.assertIsNone(low.assigned_to)
