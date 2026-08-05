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

from django.contrib.auth import get_user_model
from django.contrib.messages.middleware import MessageMiddleware
from django.contrib.sessions.middleware import SessionMiddleware
from django.test import RequestFactory, TestCase

from assets.admin.asset_planning_request import AssetPlanningRequestAdmin
from assets.models import (
    Asset, AssetPlanningRequest, AssetPlanningRequestStatus, AssetType, AssetTypeCategory,
)
from company.models import Company
from management.models import ProjectFile
from sales.models import Client
from api.fleet.tests.helpers import make_asset_type, make_company, make_employee, make_user, make_zone

User = get_user_model()
NEEDED_DATE = date(2026, 8, 10)


class ApproveCopiesSuggestedTrucksTests(TestCase):
    def setUp(self):
        self.company = make_company()
        self.to_zone = make_zone(self.company, name="To Zone")
        self.asset = Asset.objects.create(company=self.company, asset_type=make_asset_type(self.company), name="Machine 1")
        client = Client.objects.create(name="Client 1", company=self.company)
        self.project_file = ProjectFile.objects.create(name="Project 1", client=client, company=self.company)

        truck_type = AssetType.objects.create(company=self.company, name="CAMIONES", category=AssetTypeCategory.TRUCK)
        self.truck_a = Asset.objects.create(company=self.company, asset_type=truck_type, name="Truck A")
        self.truck_b = Asset.objects.create(company=self.company, asset_type=truck_type, name="Truck B")

        self.superuser = User.objects.create_superuser(username="admin", password="testpass123", email="a@a.com")
        make_employee(self.company, self.superuser)

        self.request_obj = AssetPlanningRequest.objects.create(
            company=self.company,
            service_type="porte_maquina",
            to_zone=self.to_zone,
            needed_date=NEEDED_DATE,
            asset=self.asset,
            project_file=self.project_file,
            estimated_duration=timedelta(hours=1),
        )
        self.request_obj.suggested_trucks.set([self.truck_a, self.truck_b])

    def _approve(self):
        request = RequestFactory().post("/")
        SessionMiddleware(lambda r: None).process_request(request)
        request.session.save()
        MessageMiddleware(lambda r: None).process_request(request)
        request.user = self.superuser
        AssetPlanningRequestAdmin(AssetPlanningRequest, admin_site=None).approve_submit_action(request, self.request_obj)

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
