"""
api/fleet/tests/helpers.py

Shared helper functions used by all test files.
Every test file imports from here instead of duplicating this code.

Usage:
    from api.fleet.tests.helpers import make_company, make_user, make_employee, make_asset_type, make_asset, make_zone
"""

from datetime import date, time

from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework.test import APIClient

from assets.models import Asset, AssetPlanning, AssetType, PlanningStatus, PlanningType
from company.models import Company, Employee, WorkingZone

User = get_user_model()


def make_company(name="Test Company"):
    """Create a minimal Company. Every other object belongs to a company."""
    return Company.objects.create(name=name)


def make_user(username="testuser", password="testpass123"):
    """Create a Django user."""
    return User.objects.create_user(username=username, password=password)


def make_employee(company, user):
    """
    Create an Employee linked to a user and a company.
    This is the object the app uses to identify who is doing the action.
    """
    return Employee.objects.create(
        company=company,
        user=user,
        first_name="Test",
        last_name="Driver",
        hired_at=date.today(),
    )


def make_asset_type(company, name="Cuba 6m3", category=None):
    """Create an AssetType (e.g. 'Cuba 6m3'). Pass category= (an
    AssetTypeCategory) for machines/trucks; omitted keeps the model default."""
    fields = {"category": category} if category is not None else {}
    return AssetType.objects.create(
        company=company,
        name=name,
        **fields,
    )


def make_asset(company, asset_type, employee=None, zone=None, name="CUBA-001"):
    """Create an Asset (a cuba) optionally assigned to an employee and zone."""
    return Asset.objects.create(
        company=company,
        asset_type=asset_type,
        name=name,
        assigned_to=employee,
        current_working_zone=zone,
    )


def make_zone(company, name="Zona Test", lat=40.416775, lon=-3.703790, confirmed=True, zone_type=None):
    """
    Create a WorkingZone with a location point.
    POINT(longitude latitude) — note lon comes first, lat second.
    This is the format django-location-field stores.

    confirmed=True (default) marks the zone's location as already
    confirmed, matching how established zones behave in production
    (strict geofence radius, no auto-relocate). Pass confirmed=False
    to test the first-transaction leniency/auto-relocate behavior.

    zone_type=None keeps the model default; pass a WorkingZone.ZoneType
    (or use make_obra / make_almacen) for a typed zone.
    """
    fields = {"zone_type": zone_type} if zone_type is not None else {}
    return WorkingZone.objects.create(
        company=company,
        name=name,
        street_address="Calle Test 1",
        city="Madrid",
        postal_code="28001",
        location=f"POINT({lon} {lat})",
        location_confirmed_at=timezone.now() if confirmed else None,
        **fields,
    )


def make_obra(company, name="Obra", **kwargs):
    """A WorkingZone of type OBRA (a client's job site)."""
    return make_zone(company, name=name, zone_type=WorkingZone.ZoneType.OBRA, **kwargs)


def make_almacen(company, name="Almacen", **kwargs):
    """A WorkingZone of type ALMACEN (the company's own yard)."""
    return make_zone(company, name=name, zone_type=WorkingZone.ZoneType.ALMACEN, **kwargs)


def make_planning(
    company, asset=None, assigned_to=None,
    planning_type=PlanningType.CUBA, status=PlanningStatus.PENDING,
    scheduled_date=None, **overrides,
):
    """Create an AssetPlanning — the plan a driver must have to operate a cuba/porte."""
    overrides.setdefault("scheduled_time", time(8, 0))
    return AssetPlanning.objects.create(
        company=company,
        asset=asset,
        assigned_to=assigned_to,
        planning_type=planning_type,
        status=status,
        scheduled_date=scheduled_date or timezone.localdate(),
        **overrides,
    )


def run_strategy(transaction_type, asset, employee, user, data=None, *, move_asset_to=None):
    """
    Run MovementStrategy / DeliveryStrategy / ReceiptStrategy directly (no HTTP),
    picked by transaction_type ("movement", "delivery" or "receipt"), and return
    its response. `data` is the rest of the payload the app would send.

    move_asset_to=<zone> first parks the asset in that zone, the way it sits
    before a driver picks it up there.
    """
    from api.fleet.views.assets import DeliveryStrategy, MovementStrategy, ReceiptStrategy

    strategies = {
        "movement": MovementStrategy,
        "delivery": DeliveryStrategy,
        "receipt": ReceiptStrategy,
    }
    if move_asset_to is not None:
        asset.current_working_zone = move_asset_to
        asset.save(update_fields=["current_working_zone"])
    payload = {"transaction_type": transaction_type, **(data or {})}
    return strategies[transaction_type](payload, asset, employee, user).execute()


class DriverFixtureMixin:
    """
    TestCase mixin: setUp builds the driver every fleet test acts as --
    self.company, self.user and self.employee (the user's Employee in that company).
    """

    def setUp(self):
        super().setUp()
        self.company = make_company()
        self.user = make_user()
        self.employee = make_employee(self.company, self.user)


class ApiFixtureMixin(DriverFixtureMixin):
    """
    DriverFixtureMixin plus an APIClient already authenticated as self.user,
    exposed as both self.api_client and self.client.
    """

    def setUp(self):
        super().setUp()
        self.api_client = APIClient()
        self.api_client.force_authenticate(user=self.user)
        self.client = self.api_client
