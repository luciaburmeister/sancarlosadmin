"""Conflict detection for planned production sessions.

Most conflicts are warnings the planner can override. Integrity violations,
locked sessions, and full unattended machines block outright.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta

from django.contrib.contenttypes.prefetch import GenericPrefetch
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext as _

from company.models import Employee, Machine
from production.models import (
    PlannedProductionSession,
    PlannedSessionResource,
    ProductionResource,
)
from production.services.working_calendar import WorkingCalendar


class ConflictCode(models.TextChoices):
    DUPLICATE_STAGE_SLOT = "duplicate_stage_slot", _("Duplicate stage time slot")
    RESOURCE_OVERLAP = "resource_overlap", _("Resource overlap")
    DAILY_CAPACITY_EXCEEDED = "daily_capacity", _("Daily capacity exceeded")
    OUTSIDE_WORKING_HOURS = "outside_hours", _("Outside working hours")
    NON_WORKING_DAY = "non_working_day", _("Non-working day")
    RESOURCE_NOT_SKILLED = "not_skilled", _("Resource cannot work this stage")
    REQUIREMENT_UNMET = "requirement_unmet", _("Missing required resources")
    STAGE_ORDER_VIOLATION = "stage_order", _("Stage order violated")
    LOCKED_SESSION = "locked", _("Session is locked")
    INVALID_INTERVAL = "invalid_interval", _("Invalid interval")
    CROSS_COMPANY = "cross_company", _("Resource belongs to another company")


BLOCK = "block"
WARN = "warn"


@dataclass(frozen=True)
class Conflict:
    code: str
    severity: str
    message: str
    resource_id: int | None = None
    session_id: int | None = None
    detail: dict = field(default_factory=dict)

    @property
    def is_blocking(self) -> bool:
        return self.severity == BLOCK

    def as_dict(self) -> dict:
        return {
            "code": self.code,
            "severity": self.severity,
            "message": self.message,
            "resource_id": self.resource_id,
            "session_id": self.session_id,
            "detail": self.detail,
        }


@dataclass
class SessionCandidate:
    """A proposed placement, whether or not it is already saved."""

    company_id: int
    stage: str
    start: datetime
    end: datetime
    resource_ids: list[int]
    ticket_id: int | None = None
    sequence: int = 0
    session_id: int | None = None
    locked: bool = False

    @classmethod
    def from_session(cls, session, *, resource_ids=None) -> SessionCandidate:
        if resource_ids is None:
            resource_ids = [
                assignment.resource_id for assignment in session.resources.all()
            ]
        return cls(
            company_id=session.company_id,
            stage=session.production_stage,
            start=session.planned_start,
            end=session.planned_end,
            resource_ids=list(resource_ids),
            ticket_id=session.production_ticket_id,
            sequence=session.sequence,
            session_id=session.pk,
            locked=session.locked,
        )

    @property
    def wall_minutes(self) -> int:
        return int((self.end - self.start).total_seconds() // 60)


def nfc(value: str) -> str:
    return unicodedata.normalize("NFC", (value or "").strip())


def blocking(conflicts) -> list[Conflict]:
    return [c for c in conflicts if c.is_blocking]


class ConflictChecker:
    """Runs the checks for one candidate. Build one per request and reuse it --
    resource rows are loaded once and cached."""

    def __init__(self, company, *, calendar: WorkingCalendar | None = None):
        self.company = company
        self.calendar = calendar or WorkingCalendar.for_company(company)
        self._resources: dict[int, ProductionResource] = {}

    # --- resource loading -------------------------------------------------

    def resources(self, resource_ids) -> dict[int, ProductionResource]:
        missing = [r for r in resource_ids if r not in self._resources]
        if missing:
            loaded = ProductionResource.objects.filter(pk__in=missing).prefetch_related(
                GenericPrefetch(
                    "target", [Employee.objects.all(), Machine.objects.all()]
                )
            )
            for resource in loaded:
                self._resources[resource.pk] = resource
        return {r: self._resources[r] for r in resource_ids if r in self._resources}

    def label(self, resource: ProductionResource) -> str:
        return str(resource.target) if resource.target else _("Resource %s") % resource.pk

    # --- entry point ------------------------------------------------------

    def check(
        self,
        candidate: SessionCandidate,
        *,
        require_explicit_stage: bool = False,
        exclude_session_ids=(),
    ) -> list[Conflict]:
        conflicts: list[Conflict] = []

        conflicts += self._check_interval(candidate)
        if blocking(conflicts):
            # An inverted interval makes every other check meaningless.
            return conflicts

        resources = self.resources(candidate.resource_ids)

        conflicts += self._check_locked(candidate)
        conflicts += self._check_company(candidate, resources)
        conflicts += self._check_calendar(candidate)
        conflicts += self._check_skills(
            candidate, resources, require_explicit_stage=require_explicit_stage
        )
        excluded = set(exclude_session_ids)
        if candidate.session_id:
            excluded.add(candidate.session_id)
        conflicts += self._check_duplicate_stage_slot(candidate, excluded)
        conflicts += self._check_overlap(candidate, resources, excluded)
        conflicts += self._check_daily_capacity(candidate, resources, excluded)
        conflicts += self._check_stage_order(candidate, excluded)

        return conflicts

    # --- individual checks ------------------------------------------------

    def resource_ids_by_capacity(self, candidate: SessionCandidate) -> list[int]:
        """Prefer free resources, then overridable warnings, then hard conflicts."""
        resources = self.resources(candidate.resource_ids)
        excluded = {candidate.session_id} if candidate.session_id else set()
        conflicts = self._check_overlap(candidate, resources, excluded)
        conflicts += self._check_daily_capacity(candidate, resources, excluded)
        priority = {}
        for conflict in conflicts:
            priority[conflict.resource_id] = max(
                priority.get(conflict.resource_id, 0), 2 if conflict.is_blocking else 1,
            )
        eligible = [
            resource_id for resource_id in candidate.resource_ids
            if resource_id in resources
        ]
        return sorted(eligible, key=lambda resource_id: priority.get(resource_id, 0))

    def _check_interval(self, candidate) -> list[Conflict]:
        if candidate.end <= candidate.start:
            return [
                Conflict(
                    code=ConflictCode.INVALID_INTERVAL,
                    severity=BLOCK,
                    message=_("The end must come after the start."),
                    session_id=candidate.session_id,
                )
            ]
        return []

    def _check_locked(self, candidate) -> list[Conflict]:
        if candidate.locked:
            return [
                Conflict(
                    code=ConflictCode.LOCKED_SESSION,
                    severity=BLOCK,
                    message=_("This session is locked. Unlock it before moving it."),
                    session_id=candidate.session_id,
                )
            ]
        return []

    def _check_company(self, candidate, resources) -> list[Conflict]:
        conflicts = []
        for resource_id, resource in resources.items():
            if resource.company_id and resource.company_id != candidate.company_id:
                conflicts.append(
                    Conflict(
                        code=ConflictCode.CROSS_COMPANY,
                        severity=BLOCK,
                        message=_("%(resource)s belongs to another company.")
                        % {"resource": self.label(resource)},
                        resource_id=resource_id,
                        session_id=candidate.session_id,
                    )
                )
        return conflicts

    def _check_calendar(self, candidate) -> list[Conflict]:
        """Flag work that *begins* or *ends* while the shop is closed.
        Spanning a break (e.g. lunch) is normal and not itself a conflict.
        """
        open_minutes = self.calendar.working_minutes_between(
            candidate.start, candidate.end
        )
        local_start = timezone.localtime(candidate.start, self.calendar.timezone)
        detail = {
            "open_minutes": open_minutes,
            "wall_minutes": candidate.wall_minutes,
        }

        if open_minutes == 0:
            if not self.calendar.windows_for_date(local_start.date()):
                return [
                    Conflict(
                        code=ConflictCode.NON_WORKING_DAY,
                        severity=WARN,
                        message=_("%(day)s is not a working day.")
                        % {"day": local_start.strftime("%A %d/%m")},
                        session_id=candidate.session_id,
                        detail=detail,
                    )
                ]
            return [self._outside_hours(candidate, detail)]

        # end is exclusive, so probe the last minute actually worked.
        starts_open = self.calendar.is_working(candidate.start)
        ends_open = self.calendar.is_working(candidate.end - timedelta(minutes=1))
        if starts_open and ends_open:
            return []

        return [self._outside_hours(candidate, detail)]

    def _outside_hours(self, candidate, detail) -> Conflict:
        return Conflict(
            code=ConflictCode.OUTSIDE_WORKING_HOURS,
            severity=WARN,
            message=_(
                "Starts or ends outside working hours (%(open)s of %(total)s "
                "minutes are open)."
            )
            % {"open": detail["open_minutes"], "total": candidate.wall_minutes},
            session_id=candidate.session_id,
            detail=detail,
        )

    def _check_skills(
        self, candidate, resources, *, require_explicit_stage: bool = False
    ) -> list[Conflict]:
        stage = nfc(candidate.stage)
        conflicts = []

        for resource_id, resource in resources.items():
            target = resource.target
            if target is None:
                continue

            allowed = {nfc(s) for s in (getattr(target, "production_stages", None) or [])}
            is_compatible = stage in allowed if require_explicit_stage else (
                not allowed or stage in allowed
            )
            if not is_compatible:
                conflicts.append(
                    Conflict(
                        code=ConflictCode.RESOURCE_NOT_SKILLED,
                        severity=WARN,
                        message=_("%(resource)s is not set up for stage '%(stage)s'.")
                        % {"resource": self.label(resource), "stage": candidate.stage},
                        resource_id=resource_id,
                        session_id=candidate.session_id,
                        detail={"stage": candidate.stage, "allowed": sorted(allowed)},
                    )
                )
            elif isinstance(target, Employee) and not target.can_work_on_production:
                conflicts.append(
                    Conflict(
                        code=ConflictCode.RESOURCE_NOT_SKILLED,
                        severity=WARN,
                        message=_("%(resource)s is not flagged as production staff.")
                        % {"resource": self.label(resource)},
                        resource_id=resource_id,
                        session_id=candidate.session_id,
                    )
                )

        return conflicts

    def _check_overlap(self, candidate, resources, exclude_session_ids) -> list[Conflict]:
        if not resources:
            return []

        overlapping = (
            PlannedSessionResource.objects.filter(
                resource_id__in=list(resources),
                planned_session__planned_start__lt=candidate.end,
                planned_session__planned_end__gt=candidate.start,
            )
            .exclude(planned_session_id__in=exclude_session_ids)
            .values_list(
                "resource_id", "planned_session__planned_start", "planned_session__planned_end"
            )
        )
        events_by_resource = {resource_id: [] for resource_id in resources}
        for resource_id, start, end in overlapping:
            events_by_resource[resource_id].extend(
                ((max(start, candidate.start), 1), (min(end, candidate.end), -1))
            )

        conflicts = []
        for resource_id, resource in resources.items():
            busy = peak = 0
            for _event_time, change in sorted(events_by_resource[resource_id]):
                busy += change
                peak = max(peak, busy)
            capacity = resource.parallel_capacity or 1
            if peak + 1 > capacity:
                conflicts.append(
                    Conflict(
                        code=ConflictCode.RESOURCE_OVERLAP,
                        severity=BLOCK if resource.counts_running_time_once else WARN,
                        message=_(
                            "%(resource)s already has %(busy)s task(s) here and "
                            "handles %(capacity)s at a time."
                        )
                        % {
                            "resource": self.label(resource),
                            "busy": peak,
                            "capacity": capacity,
                        },
                        resource_id=resource_id,
                        session_id=candidate.session_id,
                        detail={"busy": peak, "parallel_capacity": capacity},
                    )
                )
        return conflicts

    def _check_duplicate_stage_slot(self, candidate, exclude_session_ids) -> list[Conflict]:
        """Keep one ticket stage from being scheduled twice in one slot. The
        matching database constraint is the concurrency-safe backstop."""
        if candidate.ticket_id is None:
            return []

        duplicate_id = (
            PlannedProductionSession.objects.filter(
                production_ticket_id=candidate.ticket_id,
                production_stage=candidate.stage,
                planned_start=candidate.start,
                planned_end=candidate.end,
            )
            .exclude(pk__in=exclude_session_ids)
            .values_list("pk", flat=True)
            .first()
        )
        if duplicate_id is None:
            return []

        return [
            Conflict(
                code=ConflictCode.DUPLICATE_STAGE_SLOT,
                severity=BLOCK,
                message=_(
                    "This production stage is already scheduled in that time slot."
                ),
                session_id=duplicate_id,
            )
        ]

    def _check_daily_capacity(self, candidate, resources, exclude_session_ids) -> list[Conflict]:
        if not resources:
            return []

        tz = self.calendar.timezone
        days = sorted(
            {
                timezone.localtime(window.start, tz).date()
                for window in self.calendar.windows_between(candidate.start, candidate.end)
            }
        )
        if not days:
            # Fully outside working hours; _check_calendar already reported it.
            return []

        conflicts = []
        for day in days:
            day_start = datetime.combine(day, time.min).replace(tzinfo=tz)
            day_end = day_start + timedelta(days=1)

            existing = (
                PlannedSessionResource.objects.filter(
                    resource_id__in=list(resources),
                    planned_session__planned_start__lt=day_end,
                    planned_session__planned_end__gt=day_start,
                )
                .exclude(planned_session_id__in=exclude_session_ids)
                .select_related("planned_session")
            )

            used = {resource_id: 0 for resource_id in resources}
            unattended_intervals = {
                resource_id: []
                for resource_id, resource in resources.items()
                if resource.counts_running_time_once
            }
            for row in existing:
                interval = (
                    max(row.planned_session.planned_start, day_start),
                    min(row.planned_session.planned_end, day_end),
                )
                if row.resource_id in unattended_intervals:
                    unattended_intervals[row.resource_id].append(interval)
                else:
                    used[row.resource_id] += self.calendar.working_minutes_between(*interval)

            candidate_minutes = self.calendar.working_minutes_between(
                max(candidate.start, day_start), min(candidate.end, day_end)
            )

            for resource_id, resource in resources.items():
                capacity = resource.daily_capacity_minutes or 0
                if resource_id in unattended_intervals:
                    intervals = unattended_intervals[resource_id]
                    used[resource_id] = self.calendar.working_minutes_covered(intervals)
                    total = self.calendar.working_minutes_covered(
                        [*intervals, (max(candidate.start, day_start), min(candidate.end, day_end))]
                    )
                    added_minutes = total - used[resource_id]
                else:
                    total = used[resource_id] + candidate_minutes
                    added_minutes = candidate_minutes
                if capacity and total > capacity:
                    conflicts.append(
                        Conflict(
                            code=ConflictCode.DAILY_CAPACITY_EXCEEDED,
                            severity=WARN,
                            message=_(
                                "%(resource)s would reach %(total)s min on "
                                "%(day)s, over its %(capacity)s min capacity."
                            )
                            % {
                                "resource": self.label(resource),
                                "total": total,
                                "day": day.strftime("%d/%m"),
                                "capacity": capacity,
                            },
                            resource_id=resource_id,
                            session_id=candidate.session_id,
                            detail={
                                "day": day.isoformat(),
                                "used": used[resource_id],
                                "candidate": added_minutes,
                                "capacity": capacity,
                            },
                        )
                    )
        return conflicts

    def check_requirements(self, candidate, *, machine_qty, employee_qty) -> list[Conflict]:
        """Do the assigned resources cover what the stage asks for?

        Off the main :meth:`check` path since the quantities require a
        safe_eval of ``ConfigurationItem.stage_resources``; only callers that
        already resolved a stage pass them in.
        """
        resources = self.resources(candidate.resource_ids)
        machine_ct_id = self._machine_content_type_id()

        machines = sum(1 for r in resources.values() if r.content_type_id == machine_ct_id)
        employees = len(resources) - machines

        conflicts = []
        if machine_qty and machines < machine_qty:
            conflicts.append(
                Conflict(
                    code=ConflictCode.REQUIREMENT_UNMET,
                    severity=WARN,
                    message=_("Stage '%(stage)s' needs %(needed)s machine(s); %(got)s assigned.")
                    % {"stage": candidate.stage, "needed": machine_qty, "got": machines},
                    session_id=candidate.session_id,
                    detail={"kind": "machine", "needed": machine_qty, "assigned": machines},
                )
            )
        if employee_qty and employees < employee_qty:
            conflicts.append(
                Conflict(
                    code=ConflictCode.REQUIREMENT_UNMET,
                    severity=WARN,
                    message=_("Stage '%(stage)s' needs %(needed)s operator(s); %(got)s assigned.")
                    % {"stage": candidate.stage, "needed": employee_qty, "got": employees},
                    session_id=candidate.session_id,
                    detail={"kind": "employee", "needed": employee_qty, "assigned": employees},
                )
            )
        return conflicts

    @staticmethod
    def _machine_content_type_id() -> int:
        from production.services.session_resources import machine_content_type_id

        return machine_content_type_id()

    def _check_stage_order(self, candidate, exclude_session_ids) -> list[Conflict]:
        if not candidate.ticket_id:
            return []

        siblings = list(
            PlannedProductionSession.objects.filter(
                production_ticket_id=candidate.ticket_id
            )
            .exclude(pk__in=exclude_session_ids)
            .order_by("sequence", "planned_start")
            .values("id", "sequence", "production_stage", "planned_start", "planned_end")
        )

        conflicts = []

        previous = [s for s in siblings if s["sequence"] < candidate.sequence]
        following = [s for s in siblings if s["sequence"] > candidate.sequence]

        if previous:
            latest = max(previous, key=lambda s: s["planned_end"])
            if candidate.start < latest["planned_end"]:
                conflicts.append(
                    Conflict(
                        code=ConflictCode.STAGE_ORDER_VIOLATION,
                        severity=WARN,
                        message=_("Starts before '%(stage)s' finishes.")
                        % {"stage": latest["production_stage"]},
                        session_id=candidate.session_id,
                        detail={
                            "neighbour": "previous",
                            "neighbour_session_id": latest["id"],
                            "neighbour_stage": latest["production_stage"],
                        },
                    )
                )

        if following:
            earliest = min(following, key=lambda s: s["planned_start"])
            if candidate.end > earliest["planned_start"]:
                conflicts.append(
                    Conflict(
                        code=ConflictCode.STAGE_ORDER_VIOLATION,
                        severity=WARN,
                        message=_("Ends after '%(stage)s' has started.")
                        % {"stage": earliest["production_stage"]},
                        session_id=candidate.session_id,
                        detail={
                            "neighbour": "next",
                            "neighbour_session_id": earliest["id"],
                            "neighbour_stage": earliest["production_stage"],
                        },
                    )
                )

        return conflicts
