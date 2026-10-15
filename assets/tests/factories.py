"""
assets/tests/factories.py

Small fixture builders shared by the assets test modules. Generic company /
user / zone / asset helpers live in api/fleet/tests/helpers.py.
"""
from datetime import time, timedelta

from django.contrib.auth import get_user_model
from django.contrib.messages.middleware import MessageMiddleware
from django.contrib.sessions.middleware import SessionMiddleware
from django.test import RequestFactory
from django.utils import timezone

from api.fleet.tests.helpers import make_asset_type, make_company, make_employee, make_zone
from assets.admin.asset_planning_request import AssetPlanningRequestAdmin
from assets.models import Asset, AssetPlanning, AssetPlanningRequest, AssetType, AssetTypeCategory
from management.models import ProjectFile
from sales.models import Client

User = get_user_model()


def make_admin_post_request(user, path="/", data=None):
    """POST request with session + messages wired up, as admin actions expect."""
    request = RequestFactory().post(path, data or {})
    SessionMiddleware(lambda r: None).process_request(request)
    request.session.save()
    MessageMiddleware(lambda r: None).process_request(request)
    request.user = user
    return request


def approve_request(request_obj, user):
    """Run the admin approve action on an AssetPlanningRequest as `user`."""
    admin = AssetPlanningRequestAdmin(AssetPlanningRequest, admin_site=None)
    admin.approve_submit_action(make_admin_post_request(user), request_obj)


def make_truck_type(company, *, name="CAMIONES"):
    """AssetType in the TRUCK category."""
    return AssetType.objects.create(company=company, name=name, category=AssetTypeCategory.TRUCK)


def make_truck(company, *, name="Truck 1", driver=None, asset_type=None):
    """Truck asset; creates a CAMIONES truck type unless `asset_type` is given."""
    return Asset.objects.create(
        company=company,
        asset_type=asset_type or make_truck_type(company),
        name=name,
        assigned_to=driver,
    )


def make_project_file(company, *, client=None, name="Project 1", client_name="Client 1"):
    """ProjectFile, creating its Client when none is given."""
    client = client or Client.objects.create(name=client_name, company=company)
    return ProjectFile.objects.create(name=name, client=client, company=company)


def make_scheduled_planning(company, *, driver, truck, scheduled_date=None, start=time(8, 0), **fields):
    """AssetPlanning assigned to driver+truck, one hour from `start` (default 08:00 today)."""
    fields.setdefault("estimated_duration", timedelta(hours=1))
    return AssetPlanning.objects.create(
        company=company,
        assigned_to=driver,
        transport_asset=truck,
        scheduled_date=scheduled_date or timezone.localdate(),
        scheduled_time=start,
        **fields,
    )


def schedule_planning(planning, *, driver, truck, scheduled_date=None, start=time(8, 0)):
    """Make an existing planning show on the schedule: driver, truck, 1h slot today."""
    planning.assigned_to = driver
    planning.transport_asset = truck
    planning.scheduled_date = scheduled_date or timezone.localdate()
    planning.scheduled_time = start
    planning.estimated_duration = timedelta(hours=1)
    planning.save()
    return planning


class PlanningRequestFixtureMixin:
    """company, to_zone, Machine asset, project_file and an admin superuser with an employee."""

    def setUp(self):
        super().setUp()
        self.company = make_company()
        self.to_zone = make_zone(self.company, name="To Zone")
        self.asset = Asset.objects.create(
            company=self.company, asset_type=make_asset_type(self.company), name="Machine 1",
        )
        self.project_file = make_project_file(self.company)
        self.superuser = User.objects.create_superuser(username="admin", password="testpass123", email="a@a.com")
        self.superuser_employee = make_employee(self.company, self.superuser)
