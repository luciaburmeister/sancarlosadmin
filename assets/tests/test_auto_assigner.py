"""
assets/tests/test_auto_assigner.py

Tests for SimpleAssigner (v1 greedy auto-assignment of driver+truck to
pending AssetPlanning records). See assets/services/auto_assigner.py.

Run just this file:
    python manage.py test assets.tests.test_auto_assigner
"""
from datetime import date, time, timedelta
from unittest.mock import patch

from django.test import TestCase

from assets.models import Asset, AssetPlanning, AssetType, AssetTypeCategory, PlanningUrgency
from assets.services.auto_assigner import SimpleAssigner
from api.fleet.tests.helpers import make_asset_type, make_company, make_employee, make_user, make_zone

TODAY = date(2026, 8, 10)


def make_truck_type(company, name="CAMIONES"):
    return AssetType.objects.create(company=company, name=name, category=AssetTypeCategory.TRUCK)


def make_truck(company, truck_type, name="Truck 1"):
    return Asset.objects.create(company=company, asset_type=truck_type, name=name)


def make_planning(company, origin, destination, urgency=PlanningUrgency.MEDIUM, scheduled_time=None,
                   scheduled_end_time=None, planning_type="porte", cuba_size="", assigned_to=None,
                   transport_asset=None):
    return AssetPlanning.objects.create(
        company=company,
        asset=Asset.objects.create(company=company, asset_type=make_asset_type(company, "Cuba"), name="Cuba 1"),
        origin=origin,
        destination=destination,
        scheduled_date=TODAY,
        scheduled_time=scheduled_time,
        scheduled_end_time=scheduled_end_time,
        urgency=urgency,
        planning_type=planning_type,
        cuba_size=cuba_size,
        assigned_to=assigned_to,
        transport_asset=transport_asset,
    )


# All tests patch route duration to a fixed 1h so they don't depend on
# GOOGLE_MAPS_API_KEY / network access.
@patch("assets.services.auto_assigner.GoogleRouteOptimizer.estimate_duration", return_value=timedelta(hours=1))
class SimpleAssignerTests(TestCase):
    def setUp(self):
        self.company = make_company()
        self.origin = make_zone(self.company, name="Origin")
        self.destination = make_zone(self.company, name="Destination")
        self.truck_type = make_truck_type(self.company)
        self.truck = make_truck(self.company, self.truck_type, name="Truck 1")
        user = make_user(username="driver1")
        self.driver = make_employee(self.company, user)

    def test_assigns_available_driver_and_truck(self, _mock):
        planning = make_planning(self.company, self.origin, self.destination)

        result = SimpleAssigner().assign_for_date(TODAY, self.company.id)

        self.assertEqual(result.assigned, [planning])
        planning.refresh_from_db()
        self.assertEqual(planning.transport_asset, self.truck)
        self.assertEqual(planning.assigned_to, self.driver)
        self.assertEqual(planning.scheduled_time, time(7, 0))
        self.assertEqual(planning.scheduled_end_time, time(8, 0))

    def test_skips_when_no_active_driver(self, _mock):
        self.driver.is_active = False
        self.driver.save()
        planning = make_planning(self.company, self.origin, self.destination)

        result = SimpleAssigner().assign_for_date(TODAY, self.company.id)

        self.assertEqual(result.assigned, [])
        self.assertEqual(result.skipped, [planning])
        planning.refresh_from_db()
        self.assertIsNone(planning.transport_asset)
        self.assertIsNone(planning.assigned_to)

    def test_no_double_booking_same_driver(self, _mock):
        # Existing planning occupies the driver from 07:00 to 08:00.
        make_planning(
            self.company, self.origin, self.destination,
            scheduled_time=time(7, 0), scheduled_end_time=time(8, 0),
            assigned_to=self.driver, transport_asset=self.truck,
        )
        new_planning = make_planning(self.company, self.origin, self.destination)

        result = SimpleAssigner().assign_for_date(TODAY, self.company.id)

        new_planning.refresh_from_db()
        self.assertEqual(result.assigned, [new_planning])
        self.assertEqual(new_planning.assigned_to, self.driver)
        # Bumped to the next free slot instead of overlapping 07:00-08:00.
        self.assertEqual(new_planning.scheduled_time, time(8, 0))

    def test_urgency_priority_wins_the_desired_slot(self, _mock):
        # Only one driver/truck available; both requests want the exact same
        # explicit time window. The urgent one must get it, the other is skipped
        # (an explicitly requested time that conflicts isn't shifted, per design).
        low = make_planning(
            self.company, self.origin, self.destination,
            urgency=PlanningUrgency.LOW, scheduled_time=time(9, 0), scheduled_end_time=time(10, 0),
        )
        urgent = make_planning(
            self.company, self.origin, self.destination,
            urgency=PlanningUrgency.URGENT, scheduled_time=time(9, 0), scheduled_end_time=time(10, 0),
        )

        result = SimpleAssigner().assign_for_date(TODAY, self.company.id)

        urgent.refresh_from_db()
        low.refresh_from_db()
        self.assertIn(urgent, result.assigned)
        self.assertEqual(urgent.assigned_to, self.driver)
        self.assertIn(low, result.skipped)
        self.assertIsNone(low.assigned_to)

    def test_respects_suggested_trucks_restriction(self, _mock):
        # Sorts before self.truck by name — would be picked first if the
        # restriction weren't applied.
        make_truck(self.company, self.truck_type, name="Truck 0")
        planning = make_planning(self.company, self.origin, self.destination)
        planning.suggested_trucks.set([self.truck])

        result = SimpleAssigner().assign_for_date(TODAY, self.company.id)

        self.assertEqual(result.assigned, [planning])
        planning.refresh_from_db()
        self.assertEqual(planning.transport_asset, self.truck)

    def test_empty_suggested_trucks_allows_any_truck(self, _mock):
        planning = make_planning(self.company, self.origin, self.destination)
        self.assertEqual(planning.suggested_trucks.count(), 0)

        result = SimpleAssigner().assign_for_date(TODAY, self.company.id)

        self.assertEqual(result.assigned, [planning])
        planning.refresh_from_db()
        self.assertEqual(planning.transport_asset, self.truck)

    def test_explicit_time_and_flexible_time_priority(self, _mock):
        # Same urgency: the one with an explicit time must be processed (and
        # win the single available driver) before the date-only one.
        flexible = make_planning(self.company, self.origin, self.destination)
        timed = make_planning(
            self.company, self.origin, self.destination,
            scheduled_time=time(11, 0), scheduled_end_time=time(12, 0),
        )

        result = SimpleAssigner().assign_for_date(TODAY, self.company.id)

        timed.refresh_from_db()
        flexible.refresh_from_db()
        self.assertEqual(timed.assigned_to, self.driver)
        self.assertEqual(timed.scheduled_time, time(11, 0))
        # Flexible one still gets assigned too (no conflict — different slot).
        self.assertEqual(flexible.assigned_to, self.driver)
        self.assertNotEqual(flexible.scheduled_time, time(11, 0))
