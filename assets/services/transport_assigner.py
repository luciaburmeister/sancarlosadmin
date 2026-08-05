"""
Transport assignment for AssetPlanning.

Core idea: truck first, driver derived from the truck — not an independent
driver+truck pair search. For every AssetPlanning that needs a truck and/or
driver:

1. Pick a candidate truck: transport_asset if already set, else narrowed by
   suggested_trucks (or every active truck in the company), filtered by
   CompatibilityChecker (TransportCompatibilityRule + Asset.excluded_assets).
2. The driver comes from that truck's own link, not an independent search:
   Asset.assigned_to first; if unavailable (schedule conflict), fall back to
   employees authorized for that truck (MachinePermission), least busy that
   day first.
3. Time: duration (AssetPlanning.estimated_duration) never changes once set —
   it's only estimated (GoogleRouteOptimizer) the first time, when
   estimated_duration is still blank. Position (start time) can shift within
   the same day to find a free slot, unless AssetPlanning.time_locked pins it
   to the exact existing slot. The day itself (scheduled_date) never moves.

AssetPlanning.skip_auto_assignment opts a planning out entirely, for both
entry points below.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

from django.db import transaction
from django.db.models import Count, Q

from company.models import Employee

from assets.models import (
    Asset,
    AssetPlanning,
    AssetTypeCategory,
    AssignmentOrigin,
    PlanningUrgency,
)
from assets.services.compatibility import CompatibilityChecker
from assets.services.route_optimizer import GoogleRouteOptimizer

WORKING_DAY_START = time(7, 0)
WORKING_DAY_END = time(17, 0)
SLOT_STEP = timedelta(minutes=15)

URGENCY_ORDER = {
    PlanningUrgency.URGENT: 0,
    PlanningUrgency.HIGH: 1,
    PlanningUrgency.MEDIUM: 2,
    PlanningUrgency.LOW: 3,
}

ASSIGNABLE_FIELDS = ["transport_asset", "assigned_to", "scheduled_time", "estimated_duration", "assignment_origin"]


def _combine(t: time) -> datetime:
    return datetime.combine(date.today(), t)


def _add_time(t: time, delta: timedelta) -> time | None:
    """None if the result would cross into a different day — no valid same-day slot."""
    result = _combine(t) + delta
    return result.time() if result.date() == date.today() else None


def _sub_time(t: time, delta: timedelta) -> time | None:
    result = _combine(t) - delta
    return result.time() if result.date() == date.today() else None


@dataclass
class AssignmentResult:
    assigned: list[AssetPlanning] = field(default_factory=list)
    skipped: list[AssetPlanning] = field(default_factory=list)


class TransportAssigner:
    # ── Public entry points ─────────────────────────────────────────────
    def assign_one(self, planning: AssetPlanning) -> bool:
        """Resolve a single planning, treating every other planning as fixed. Returns whether it was assigned."""
        with transaction.atomic():
            planning = AssetPlanning.objects.select_for_update().get(pk=planning.pk)
            return self._resolve_and_save(planning)

    def assign_bulk(self, dates, company_id, reassign_all: bool = False) -> AssignmentResult:
        """Resolve every eligible pending planning across `dates` for one company."""
        result = AssignmentResult()
        with transaction.atomic():
            # select_related is deliberately omitted — combining it with
            # select_for_update() on these nullable FKs makes Postgres emit a
            # LEFT JOIN, which FOR UPDATE cannot lock.
            qs = (
                AssetPlanning.objects.select_for_update()
                .filter(company_id=company_id, scheduled_date__in=list(dates), status="pending")
                .exclude(skip_auto_assignment=True)
            )
            if not reassign_all:
                qs = qs.filter(
                    Q(assigned_to__isnull=True) | Q(transport_asset__isnull=True) | Q(scheduled_time__isnull=True)
                )
            pending = list(qs)
            pending.sort(key=self._priority_key)

            for planning in pending:
                if reassign_all and (planning.assigned_to_id or planning.transport_asset_id):
                    # Reset the assignment, never the schedule — see module docstring.
                    planning.transport_asset = None
                    planning.assigned_to = None
                    planning.assignment_origin = AssignmentOrigin.UNASSIGNED
                    planning.save(update_fields=["transport_asset", "assigned_to", "assignment_origin"])

                if self._resolve_and_save(planning):
                    result.assigned.append(planning)
                else:
                    result.skipped.append(planning)
        return result

    # ── Shared resolve+save ─────────────────────────────────────────────
    def _resolve_and_save(self, planning: AssetPlanning) -> bool:
        if planning.skip_auto_assignment:
            return False
        resolved = self._resolve(planning)
        if resolved is None:
            return False
        truck, driver, start, end = resolved
        planning.transport_asset = truck
        planning.assigned_to = driver
        planning.scheduled_time = start
        planning.estimated_duration = _combine(end) - _combine(start)
        planning.assignment_origin = AssignmentOrigin.AUTO
        planning.save(update_fields=ASSIGNABLE_FIELDS)
        return True

    # ── Ordering: urgency, then whether a time was actually requested ──
    @staticmethod
    def _priority_key(planning: AssetPlanning):
        return (
            URGENCY_ORDER.get(planning.urgency, 2),
            0 if planning.scheduled_time else 1,
            planning.scheduled_time or time.min,
            planning.pk,
        )

    # ── Core resolution ──────────────────────────────────────────────────
    def _resolve(self, planning: AssetPlanning):
        duration = self._duration(planning)
        for truck, driver in self._candidate_pairs(planning):
            slot = self._resolve_time(planning, truck, driver, duration)
            if slot is not None:
                return truck, driver, slot[0], slot[1]
        return None

    @staticmethod
    def _duration(planning: AssetPlanning) -> timedelta:
        if planning.estimated_duration:
            return planning.estimated_duration
        return GoogleRouteOptimizer().estimate_duration(planning.origin, planning.destination)

    # ── Candidate (truck, driver) pairs — the truck<->employee link decides
    # the driver, not an independent search. Whichever side is already fixed
    # on the planning narrows the other. ──────────────────────────────────
    def _candidate_pairs(self, planning: AssetPlanning):
        if planning.transport_asset_id and planning.assigned_to_id:
            yield planning.transport_asset, planning.assigned_to
            return
        if planning.transport_asset_id:
            for driver in self._drivers_for_truck(planning, planning.transport_asset):
                yield planning.transport_asset, driver
            return
        if planning.assigned_to_id:
            for truck in self._trucks_for_driver(planning, planning.assigned_to):
                yield truck, planning.assigned_to
            return
        checker = CompatibilityChecker(planning)
        for truck in self._truck_candidates(planning, checker):
            for driver in self._drivers_for_truck(planning, truck):
                yield truck, driver

    def _truck_candidates(self, planning: AssetPlanning, checker: CompatibilityChecker):
        qs = Asset.objects.filter(
            asset_type__category=AssetTypeCategory.TRUCK,
            is_active=True,
            company_id=planning.company_id,
        )
        suggested_ids = list(planning.suggested_trucks.values_list("pk", flat=True))
        if suggested_ids:
            qs = qs.filter(pk__in=suggested_ids)
        return [t for t in qs.order_by("name") if checker.is_compatible(t)]

    def _trucks_for_driver(self, planning: AssetPlanning, driver: Employee):
        checker = CompatibilityChecker(planning)
        own = Asset.objects.filter(
            assigned_to=driver, asset_type__category=AssetTypeCategory.TRUCK,
            is_active=True, company_id=planning.company_id,
        ).order_by("name")
        authorized = Asset.objects.filter(
            machine_permissions__employee=driver, asset_type__category=AssetTypeCategory.TRUCK,
            is_active=True, company_id=planning.company_id,
        ).exclude(assigned_to=driver).order_by("name")
        trucks = list(own) + list(authorized)
        suggested_ids = set(planning.suggested_trucks.values_list("pk", flat=True))
        if suggested_ids:
            trucks = [t for t in trucks if t.pk in suggested_ids]
        return [t for t in trucks if checker.is_compatible(t)]

    def _drivers_for_truck(self, planning: AssetPlanning, truck: Asset):
        candidates = []
        primary = truck.assigned_to
        if primary is not None and primary.is_active and primary.company_id == planning.company_id:
            candidates.append(primary)

        authorized = list(
            Employee.objects.filter(
                machine_permissions__asset_id=truck.pk,
                is_active=True,
                company_id=planning.company_id,
            ).exclude(pk=primary.pk if primary else None).distinct()
        )
        workload = self._workload_by_employee(planning)
        authorized.sort(key=lambda e: workload.get(e.pk, 0))
        candidates.extend(authorized)
        return candidates

    @staticmethod
    def _workload_by_employee(planning: AssetPlanning) -> dict:
        """How many plannings each employee already has that day — the tiebreak for authorized alternates."""
        rows = (
            AssetPlanning.objects.filter(company_id=planning.company_id, scheduled_date=planning.scheduled_date)
            .exclude(status="cancelled")
            .exclude(pk=planning.pk)
            .values("assigned_to")
            .annotate(n=Count("id"))
        )
        return {row["assigned_to"]: row["n"] for row in rows if row["assigned_to"] is not None}

    # ── Time resolution for one (truck, driver) candidate ────────────────
    def _resolve_time(self, planning: AssetPlanning, truck: Asset, driver: Employee, duration: timedelta):
        if planning.scheduled_time:
            start = planning.scheduled_time
            end = _add_time(start, duration)
            if end is None:
                return None
            if not self._has_conflict(planning, truck, driver, start, end):
                return start, end
            if planning.time_locked:
                return None
            return self._first_free_slot(planning, truck, driver, duration)

        return self._first_free_slot(planning, truck, driver, duration)

    @staticmethod
    def _has_conflict(planning: AssetPlanning, truck: Asset, driver: Employee, start: time, end: time) -> bool:
        return AssetPlanning.has_overlap(
            scheduled_date=planning.scheduled_date,
            start=start, end=end,
            assigned_to_id=driver.pk, transport_asset_id=truck.pk,
            exclude_pk=planning.pk,
        )

    def _first_free_slot(self, planning: AssetPlanning, truck: Asset, driver: Employee, duration: timedelta):
        cursor = WORKING_DAY_START
        limit = _sub_time(WORKING_DAY_END, duration)
        if limit is None:
            return None
        while cursor <= limit:
            end = _add_time(cursor, duration)
            if end is not None and not self._has_conflict(planning, truck, driver, cursor, end):
                return cursor, end
            cursor = _add_time(cursor, SLOT_STEP)
            if cursor is None:
                break
        return None
