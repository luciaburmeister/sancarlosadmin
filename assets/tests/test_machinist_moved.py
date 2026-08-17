"""
assets/tests/test_machinist_moved.py

Tests for the "machinist moved" dynamic field pair on both
AssetPlanningRequest and AssetPlanning:
  - machinist_moved (bool) — only meaningful for Own Machine Transport,
    reset to False otherwise (see AssetPlanningRequest.clean()).
  - moved_employees (M2M) — who moved, shown only when machinist_moved is set.

See assets/models.py, assets/admin/asset_planning_request.py,
assets/admin/asset_planning.py.

Run just this file:
    python manage.py test assets.tests.test_machinist_moved
"""
from datetime import date, timedelta

from django.contrib.auth import get_user_model
from django.contrib.messages.middleware import MessageMiddleware
from django.contrib.sessions.middleware import SessionMiddleware
from django.test import RequestFactory, TestCase

from assets.admin.asset_planning_request import AssetPlanningRequestAdmin
from assets.models import Asset, AssetPlanningRequest, AssetType, AssetTypeCategory
from company.models import Company
from management.models import ProjectFile
from sales.models import Client
from api.fleet.tests.helpers import make_asset_type, make_company, make_employee, make_user, make_zone

User = get_user_model()
NEEDED_DATE = date(2026, 8, 10)


class MachinistMovedCleanTests(TestCase):
    def setUp(self):
        self.company = make_company()
        self.to_zone = make_zone(self.company, name="To Zone")
        client = Client.objects.create(name="Client 1", company=self.company)
        self.project_file = ProjectFile.objects.create(name="Project 1", client=client, company=self.company)

    def test_reset_to_false_when_service_type_is_not_maquina_propia(self):
        request = AssetPlanningRequest(
            company=self.company,
            service_type="servicio_grua",
            to_zone=self.to_zone,
            needed_date=NEEDED_DATE,
            project_file=self.project_file,
            machinist_moved=True,
            estimated_duration=timedelta(hours=1),
        )
        request.clean()
        self.assertFalse(request.machinist_moved)

    def test_kept_when_service_type_is_maquina_propia(self):
        asset = Asset.objects.create(company=self.company, asset_type=make_asset_type(self.company), name="Machine 1")
        from_zone = make_zone(self.company, name="From Zone")
        request = AssetPlanningRequest(
            company=self.company,
            service_type="porte_maquina",
            to_zone=self.to_zone,
            from_zone=from_zone,
            needed_date=NEEDED_DATE,
            asset=asset,
            project_file=self.project_file,
            machinist_moved=True,
            estimated_duration=timedelta(hours=1),
        )
        request.clean()
        self.assertTrue(request.machinist_moved)


class ApproveCopiesMachinistMovedTests(TestCase):
    def setUp(self):
        self.company = make_company()
        self.to_zone = make_zone(self.company, name="To Zone")
        self.asset = Asset.objects.create(company=self.company, asset_type=make_asset_type(self.company), name="Machine 1")
        client = Client.objects.create(name="Client 1", company=self.company)
        self.project_file = ProjectFile.objects.create(name="Project 1", client=client, company=self.company)

        self.superuser = User.objects.create_superuser(username="admin", password="testpass123", email="a@a.com")
        make_employee(self.company, self.superuser)

        self.mover_1 = make_employee(self.company, make_user(username="mover1"))
        self.mover_2 = make_employee(self.company, make_user(username="mover2"))

        self.request_obj = AssetPlanningRequest.objects.create(
            company=self.company,
            service_type="porte_maquina",
            to_zone=self.to_zone,
            needed_date=NEEDED_DATE,
            asset=self.asset,
            project_file=self.project_file,
            machinist_moved=True,
            estimated_duration=timedelta(hours=1),
        )
        self.request_obj.moved_employees.set([self.mover_1, self.mover_2])

    def _approve(self):
        request = RequestFactory().post("/")
        SessionMiddleware(lambda r: None).process_request(request)
        request.session.save()
        MessageMiddleware(lambda r: None).process_request(request)
        request.user = self.superuser
        AssetPlanningRequestAdmin(AssetPlanningRequest, admin_site=None).approve_submit_action(request, self.request_obj)

    def test_approving_copies_machinist_moved_and_employees(self):
        self._approve()
        planning = self.request_obj.resulting_planning
        self.assertIsNotNone(planning)
        self.assertTrue(planning.machinist_moved)
        self.assertCountEqual(list(planning.moved_employees.all()), [self.mover_1, self.mover_2])

    def test_approving_with_machinist_not_moved_leaves_employees_empty(self):
        self.request_obj.machinist_moved = False
        self.request_obj.moved_employees.clear()
        self.request_obj.save()
        self._approve()
        planning = self.request_obj.resulting_planning
        self.assertFalse(planning.machinist_moved)
        self.assertEqual(planning.moved_employees.count(), 0)

    def test_duplicate_action_copies_moved_employees(self):
        request = RequestFactory().post("/")
        SessionMiddleware(lambda r: None).process_request(request)
        request.session.save()
        MessageMiddleware(lambda r: None).process_request(request)
        request.user = self.superuser

        admin_instance = AssetPlanningRequestAdmin(AssetPlanningRequest, admin_site=None)
        admin_instance.duplicate_action(request, self.request_obj.pk)

        duplicate = AssetPlanningRequest.objects.exclude(pk=self.request_obj.pk).get()
        self.assertTrue(duplicate.machinist_moved)
        self.assertCountEqual(list(duplicate.moved_employees.all()), [self.mover_1, self.mover_2])
