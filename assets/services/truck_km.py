"""Truck km log: which truck each driver is in, and the odometer readings
taken when they get in and out of it (see TruckKmSession)."""
import re

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from assets.models import (
    Asset, AssetIdentifierLabel, AssetTypeCategory, PlanningStatus, TruckKmEndReason, TruckKmSession,
)

# Bigger jumps than this between two readings of the same truck are treated
# as typos rather than a long trip.
MAX_KM_JUMP = 2000
APP_VERSION_HEADER = "HTTP_X_APP_VERSION"


class TruckKmError(Exception):
    def __init__(self, code, message, status=400, **extra):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.extra = extra

    def as_payload(self):
        return {"error": self.message, "code": self.code, **self.extra}


def normalize_plate(value):
    return re.sub(r"[\s\-.]", "", value or "").upper()


def requires_truck(employee):
    return bool(employee is not None and employee.type_id and employee.type.requires_truck)


def company_trucks(company):
    return Asset.objects.filter(
        company=company, asset_type__category=AssetTypeCategory.TRUCK, is_active=True,
    ).prefetch_related("identifiers")


def find_truck(company, truck_id=None, matricula=None):
    trucks = company_trucks(company)
    if truck_id:
        try:
            return trucks.get(id=int(truck_id))
        except (Asset.DoesNotExist, TypeError, ValueError):
            raise TruckKmError("truck_not_found", "Camión no encontrado.", status=404)
    plate = normalize_plate(matricula)
    if not plate:
        raise TruckKmError("truck_not_found", "Debes elegir un camión o escribir su matrícula.", status=404)
    for truck in trucks.filter(identifiers__label__iexact=AssetIdentifierLabel.PLATE).distinct():
        if normalize_plate(truck.matricula) == plate:
            return truck
    raise TruckKmError("truck_not_found", f"No hay ningún camión con matrícula {matricula}.", status=404)


def truck_payload(truck):
    if truck is None:
        return None
    return {"id": truck.id, "name": truck.name, "matricula": truck.matricula}


def session_payload(session):
    if session is None:
        return None
    return {
        "id": session.id,
        "truck_id": session.truck_id,
        "name": session.truck.name,
        "matricula": session.truck.matricula,
        "start_km": session.start_km,
        "start_at": session.start_at.isoformat(),
    }


def _parse_km(value):
    try:
        km = int(value)
    except (TypeError, ValueError):
        raise TruckKmError("km_invalid", "Introduce los kilómetros del camión.")
    if km < 0:
        raise TruckKmError("km_invalid", "Los kilómetros no pueden ser negativos.")
    return km


def _check_km(truck, km, confirm_lower, floor=None):
    """floor: a reading taken earlier in the same request (the start of a
    session being closed), which the new reading must not go below either."""
    last = floor if floor is not None else TruckKmSession.last_km(truck)
    if last is None:
        return
    if km - last > MAX_KM_JUMP:
        raise TruckKmError(
            "km_invalid", f"{km} km es demasiado respecto a la última lectura ({last} km). Revisa el número.",
            last_km=last,
        )
    if km < last and not confirm_lower:
        raise TruckKmError(
            "km_lower_than_last", f"La última lectura de este camión fue {last} km. ¿Seguro que son {km} km?",
            status=409, last_km=last,
        )


@transaction.atomic
def start_session(driver, truck, km, *, confirm_lower=False, confirm_takeover=False, attendance=None, planning=None):
    km = _parse_km(km)
    current = TruckKmSession.open_for(driver)
    if current is not None:
        if current.truck_id == truck.id:
            return current
        raise TruckKmError(
            "truck_already_open", f"Ya tienes abierto el camión {current.truck.matricula or current.truck.name}. Ciérralo primero.",
            status=409, open_session=session_payload(current),
        )
    holder_session = (
        TruckKmSession.objects.select_for_update().select_related("driver")
        .filter(truck=truck, end_at__isnull=True).first()
    )
    if holder_session is not None:
        if not confirm_takeover:
            raise TruckKmError(
                "truck_in_use", f"Este camión lo tiene {holder_session.driver.full_name}. ¿Continuar igualmente?",
                status=409, holder=holder_session.driver.full_name,
            )
        holder_session.close(None, TruckKmEndReason.TAKEN_OVER)
    _check_km(truck, km, confirm_lower)
    return TruckKmSession.objects.create(
        company=truck.company, truck=truck, driver=driver, attendance=attendance, planning=planning,
        start_km=km, start_at=timezone.now(),
    )


@transaction.atomic
def end_session(driver, km, *, confirm_lower=False, reason=TruckKmEndReason.CLOCK_OUT):
    """Close the driver's open truck. km=None (an app that doesn't ask for
    it) closes it without a reading, which leaves it flagged for the admin."""
    session = TruckKmSession.open_for(driver)
    if session is None:
        return None
    if km is not None:
        km = _parse_km(km)
        _check_km(session.truck, km, confirm_lower, floor=session.start_km)
    session.close(km, reason)
    return session


@transaction.atomic
def switch(driver, end_km, truck, start_km, *, confirm_lower=False, confirm_takeover=False, planning=None):
    current = TruckKmSession.open_for(driver)
    if current is not None and current.truck_id == truck.id:
        return current
    attendance = current.attendance if current is not None else None
    end_session(driver, end_km, confirm_lower=confirm_lower, reason=TruckKmEndReason.TRUCK_CHANGE)
    return start_session(
        driver, truck, start_km, confirm_lower=confirm_lower, confirm_takeover=confirm_takeover,
        attendance=attendance, planning=planning,
    )


def planning_truck_mismatch(planning, employee, open_truck_id):
    """True when this driver must change truck before starting the planning.
    Only the first action is gated, so a job already under way is never
    blocked halfway."""
    return bool(
        planning.transport_asset_id
        and planning.status == PlanningStatus.PENDING
        and requires_truck(employee)
        and open_truck_id != planning.transport_asset_id
    )


def enforces_truck_check(request):
    """Only app versions that have the truck change screen can get past a
    mismatch, so older ones are never blocked. TRUCK_KM_MIN_APP_VERSION
    unset/0 disables the server check entirely."""
    min_version = getattr(settings, "TRUCK_KM_MIN_APP_VERSION", 0)
    if not min_version:
        return False
    try:
        return int(request.META.get(APP_VERSION_HEADER, "")) >= min_version
    except ValueError:
        return False


def check_planning_truck(planning, employee):
    """Raise truck_mismatch if the driver is not in the planning's truck.
    Callers only run this when enforces_truck_check(request) is true."""
    current = TruckKmSession.open_for(employee)
    open_truck_id = current.truck_id if current is not None else None
    if planning_truck_mismatch(planning, employee, open_truck_id):
        required = planning.transport_asset
        raise TruckKmError(
            "truck_mismatch",
            f"Esta tarea requiere el camión {required.matricula or required.name}. Haz el cambio de camión primero.",
            status=409, planning_id=planning.id, required_truck=truck_payload(required),
            current_truck=truck_payload(current.truck if current is not None else None),
        )
