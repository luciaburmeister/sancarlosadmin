"""
assets/tests/test_suggested_trucks.py

Tests for the "Suggested Trucks" restriction: present on both
AssetPlanningRequest and AssetPlanning, copied over on approval, and
respected (or not, when empty) by SimpleAssigner. See:
  - assets/models.py (AssetPlanning.suggested_trucks)
  - assets/admin/asset_planning_request.py (approve_submit_action)
  - assets/services/auto_assigner.py (SimpleAssigner._truck_candidates)

Run just this file:
    python manage.py test assets.tests.test_suggested_trucks
"""
from datetime import date, timedelta

from django.test import TestCase

from assets.models import AssetPlanningRequest, AssetPlanningRequestStatus
from assets.tests.factories import PlanningRequestFixtureMixin, approve_request, make_truck, make_truck_type

NEEDED_DATE = date(2026, 8, 10)


class ApproveCopiesSuggestedTrucksTests(PlanningRequestFixtureMixin, TestCase):
    def setUp(self):
        super().setUp()
        truck_type = make_truck_type(self.company)
        self.truck_a = make_truck(self.company, name="Truck A", asset_type=truck_type)
        self.truck_b = make_truck(self.company, name="Truck B", asset_type=truck_type)

        self.request_obj = AssetPlanningRequest.objects.create(
            company=self.company,
            service_type="porte_maquina",
            to_zone=self.to_zone,
            needed_date=NEEDED_DATE,
            asset=self.asset,
            machine=self.asset,
            project_file=self.project_file,
            estimated_duration=timedelta(hours=1),
        )
        self.request_obj.suggested_trucks.set([self.truck_a, self.truck_b])

    def _approve(self):
        approve_request(self.request_obj, self.superuser)

    def test_approving_copies_suggested_trucks_to_resulting_planning(self):
        self._approve()
        self.request_obj.refresh_from_db()
        self.assertEqual(self.request_obj.status, AssetPlanningRequestStatus.APPROVED)
        planning = self.request_obj.resulting_planning
        self.assertIsNotNone(planning)
        self.assertCountEqual(list(planning.suggested_trucks.all()), [self.truck_a, self.truck_b])
        self.assertEqual(planning.project_file_id, self.project_file.pk)

    def test_approving_with_no_suggested_trucks_leaves_it_empty(self):
        self.request_obj.suggested_trucks.clear()
        self._approve()
        self.request_obj.refresh_from_db()
        planning = self.request_obj.resulting_planning
        self.assertEqual(planning.suggested_trucks.count(), 0)
