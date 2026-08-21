"""
api/fleet/tests/helpers.py

Shared helper functions used by all test files.
Every test file imports from here instead of duplicating this code.

Usage:
    from api.fleet.tests.helpers import make_company, make_user, make_employee, make_asset_type, make_asset, make_zone
"""

from datetime import date

from django.contrib.auth import get_user_model
from django.utils import timezone

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


def make_asset_type(company, name="Cuba 6m3"):
    """Create an AssetType (e.g. 'Cuba 6m3')."""
    return AssetType.objects.create(
        company=company,
        name=name,
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


def make_zone(company, name="Zona Test", lat=40.416775, lon=-3.703790, confirmed=True):
    """
    Create a WorkingZone with a location point.
    POINT(longitude latitude) — note lon comes first, lat second.
    This is the format django-location-field stores.

    confirmed=True (default) marks the zone's location as already
    confirmed, matching how established zones behave in production
    (strict geofence radius, no auto-relocate). Pass confirmed=False
    to test the first-transaction leniency/auto-relocate behavior.
    """
    return WorkingZone.objects.create(
        company=company,
        name=name,
        street_address="Calle Test 1",
        city="Madrid",
        postal_code="28001",
        location=f"POINT({lon} {lat})",
        location_confirmed_at=timezone.now() if confirmed else None,
    )


def make_planning(
    company, asset=None, assigned_to=None,
    planning_type=PlanningType.CUBA, status=PlanningStatus.PENDING,
    scheduled_date=None, **overrides,
):
    """Create an AssetPlanning — the plan a driver must have to operate a cuba/porte."""
    return AssetPlanning.objects.create(
        company=company,
        asset=asset,
        assigned_to=assigned_to,
        planning_type=planning_type,
        status=status,
        scheduled_date=scheduled_date or date.today(),
        **overrides,
    )
