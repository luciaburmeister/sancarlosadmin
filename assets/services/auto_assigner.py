"""
Simple (v1) auto-assignment for AssetPlanning.

Greedy algorithm: for every pending AssetPlanning on a given date that's
missing a driver and/or a truck, picks the first active driver+truck combo
(any active employee, any active truck in the company) that doesn't overlap
another AssetPlanning of that driver/truck on the same date.

Processing order: urgency, then whether a time was actually requested
(an explicit date+time takes priority over a date-only request).

This intentionally ignores travel distance/proximity — that's left for a
later, smarter version. See assets/services/route_optimizer.py for the
(separate) daily route re-ordering step.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

from django.db import transaction
from django.db.models import Q

from company.models import Employee

from assets.models import Asset, AssetPlanning, AssetTypeCategory, AssignmentOrigin, PlanningUrgency
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


class SimpleAssigner:
    def assign_for_date(self, target_date: date, company_id: int) -> AssignmentResult:
        result = AssignmentResult()
        with transaction.atomic():
            # select_related is deliberately omitted here — combining it with
            # select_for_update() on these nullable FKs makes Postgres emit a
            # LEFT JOIN, which FOR UPDATE cannot lock.
            pending = list(
                AssetPlanning.objects.select_for_update()
                .filter(company_id=company_id, scheduled_date=target_date, status="pending")
                .filter(Q(assigned_to__isnull=True) | Q(transport_asset__isnull=True))
            )
            pending.sort(key=self._priority_key)

            for planning in pending:
                truck, driver, start, end = self._find_assignment(planning)
                if truck is None or driver is None:
                    result.skipped.append(planning)
                    continue
                planning.transport_asset = planning.transport_asset or truck
                planning.assigned_to = planning.assigned_to or driver
                planning.scheduled_time = start
                planning.scheduled_end_time = end
                planning.assignment_origin = AssignmentOrigin.AUTO
                planning.save(update_fields=[
                    "transport_asset", "assigned_to", "scheduled_time", "scheduled_end_time", "assignment_origin",
                ])
                result.assigned.append(planning)
        return result

    # ── Ordering: urgency, then explicit date+time before date-only ──
    @staticmethod
    def _priority_key(planning: AssetPlanning):
        return (
            URGENCY_ORDER.get(planning.urgency, 2),
            0 if planning.scheduled_time else 1,
            planning.scheduled_time or time.min,
            planning.pk,
        )

    # ── Candidate search ──────────────────────────────────────────────────
    def _find_assignment(self, planning: AssetPlanning):
        duration = self._estimated_duration(planning)
        desired_start, desired_end = self._desired_block(planning, duration)
        pairs = self._eligible_pairs(planning)

        for truck, driver in pairs:
            if desired_start is not None:
                if desired_end is not None and not self._has_conflict(planning.scheduled_date, planning.pk, truck, driver, desired_start, desired_end):
                    return truck, driver, desired_start, desired_end
                continue  # an explicitly requested time that conflicts doesn't get shifted — try another pair
            start, end = self._first_free_slot(planning, truck, driver, duration)
            if start is not None:
                return truck, driver, start, end
        return None, None, None, None

    def _truck_candidates(self, planning: AssetPlanning):
        if planning.transport_asset_id:
            return Asset.objects.filter(pk=planning.transport_asset_id)
        qs = Asset.objects.filter(
            asset_type__category=AssetTypeCategory.TRUCK,
            is_active=True,
            company_id=planning.company_id,
        )
        # suggested_trucks narrows the pool when set; empty means any truck is fine.
        suggested_ids = list(planning.suggested_trucks.values_list("pk", flat=True))
        if suggested_ids:
            qs = qs.filter(pk__in=suggested_ids)
        return qs.order_by("name")

    def _eligible_pairs(self, planning: AssetPlanning):
        trucks = list(self._truck_candidates(planning))
        if not trucks:
            return []
        drivers_qs = Employee.objects.filter(is_active=True, company_id=planning.company_id).order_by("pk")
        if planning.assigned_to_id:
            drivers_qs = drivers_qs.filter(pk=planning.assigned_to_id)
        drivers = list(drivers_qs)
        return [(truck, driver) for truck in trucks for driver in drivers]

    # ── Time block helpers ───────────────────────────────────────────────
    @staticmethod
    def _estimated_duration(planning: AssetPlanning) -> timedelta:
        return GoogleRouteOptimizer().estimate_duration(planning.origin, planning.destination)

    @staticmethod
    def _desired_block(planning: AssetPlanning, duration: timedelta):
        start, end = planning.scheduled_time, planning.scheduled_end_time
        if start and end:
            return start, end
        if start:
            return start, _add_time(start, duration)
        if end:
            return _sub_time(end, duration), end
        return None, None

    @staticmethod
    def _has_conflict(scheduled_date: date, exclude_pk, truck: Asset, driver, start: time, end: time) -> bool:
        return AssetPlanning.has_overlap(
            scheduled_date=scheduled_date,
            start=start,
            end=end,
            assigned_to_id=driver.pk,
            transport_asset_id=truck.pk,
            exclude_pk=exclude_pk,
        )

    def _first_free_slot(self, planning: AssetPlanning, truck: Asset, driver, duration: timedelta):
        cursor = WORKING_DAY_START
        limit = _sub_time(WORKING_DAY_END, duration)
        if limit is None:
            return None, None
        while cursor <= limit:
            end = _add_time(cursor, duration)
            if end is not None and not self._has_conflict(planning.scheduled_date, planning.pk, truck, driver, cursor, end):
                return cursor, end
            cursor = _add_time(cursor, SLOT_STEP)
            if cursor is None:
                break
        return None, None
