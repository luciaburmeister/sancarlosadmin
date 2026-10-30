"""Move/create/reorder orchestration for the weekly planning board — spans
PlannedProductionSession, Company and ProductionResource."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from production.exceptions import PlannedSessionConflictError
from production.models import PlannedProductionSession, PlannedProductionSessionSource, ProductionTicket
from production.services.session_scheduler import get_or_create_resource_for_target
from production.services.session_resources import sync_session_resources
from production.services.planning_conflicts import ConflictChecker, ConflictCode, SessionCandidate
from production.services.production_planning import ProductionPlanningService
from production.services.working_calendar import WorkingCalendar

# Nominal duration when a backlog drop doesn't specify a time.
DEFAULT_SESSION_DURATION = timedelta(minutes=60)

NO_FREE_SLOT_MESSAGE = _("No free slot found that day for this employee.")


def find_free_slot_in_day(
    *, company, primary_resource_id: int, day: date, duration: timedelta,
    exclude_pk: int | None = None, resource_ids=None, ticket_id=None, stage=None,
) -> tuple[datetime, datetime] | None:
    """Earliest contiguous working slot for every retained resource and stage part."""
    calendar = WorkingCalendar.for_company(company)
    windows = calendar.windows_for_date(day)
    if not windows:
        return None
    resource_ids = resource_ids or [primary_resource_id]
    checker = ConflictChecker(company, calendar=calendar)
    day_start = timezone.make_aware(datetime.combine(day, time.min), calendar.timezone)
    rows = checker.preload_availability(resource_ids, day_start, day_start + timedelta(days=1))
    releases = {end for pk, resource_id, start, end in rows if pk != exclude_pk}
    parts = []
    if ticket_id:
        parts = list(PlannedProductionSession.objects.filter(
            production_ticket_id=ticket_id, production_stage=stage,
            planned_start__lt=windows[-1].end, planned_end__gt=windows[0].start,
        ).exclude(pk=exclude_pk).values_list("planned_start", "planned_end"))
        releases.update(end for start, end in parts)
    for window in windows:
        starts = {window.start, *(end for end in releases if window.start < end < window.end)}
        for cursor in sorted(starts):
            end = cursor + duration
            if end > window.end or any(lo < end and hi > cursor for lo, hi in parts):
                continue
            candidate = SessionCandidate(
                company_id=company.pk, stage=stage or "", start=cursor, end=end,
                resource_ids=resource_ids, session_id=exclude_pk,
            )
            if not any(c.code == ConflictCode.RESOURCE_OVERLAP
                       for c in checker.check_resource_availability(candidate)):
                return cursor, end
    return None


@transaction.atomic
def move_session(
    *, company, session: PlannedProductionSession, employee, day: date | None,
    start_time: time | None = None, end_time: time | None = None, end_day: date | None = None,
) -> PlannedProductionSession:
    """Move/reassign a planned session; auto-snaps to a free slot if start_time is omitted.
    *end_day* lets the session span more than one calendar day — defaults to *day* (today's
    single-day behavior) when omitted."""
    session = ProductionPlanningService._lock(session.pk)
    if session is None:
        raise ValueError(_("That planning no longer exists. Reload the board."))
    if session.company_id not in (None, company.pk):
        raise ValueError(_("The booking and ticket must belong to the same company."))
    if session.locked:
        raise ValueError(_("This session is locked and can't be moved."))

    operators = session.assigned_operators
    resource = get_or_create_resource_for_target(employee or next(iter(operators), None)) if (employee or operators) else session.primary_resource
    resource_ids = [assignment.resource_id for assignment in session.resources.all()]
    if not resource_ids and session.primary_resource_id:
        resource_ids = [session.primary_resource_id]
    if employee and employee.pk not in {operator.pk for operator in operators}:
        previous_id = get_or_create_resource_for_target(operators[0]).pk if operators else None
        resource_ids = [pk for pk in resource_ids if pk != previous_id]
        resource_ids.append(resource.pk)
    ProductionPlanningService.lock_resources(resource_ids)
    duration = session.planned_end - session.planned_start
    calendar = WorkingCalendar.for_company(company)
    split_stage = session.production_ticket_id and PlannedProductionSession.objects.filter(
        production_ticket_id=session.production_ticket_id, production_stage=session.production_stage,
    ).exclude(pk=session.pk).exists()
    if split_stage and calendar.has_slots:
        duration = timedelta(minutes=calendar.working_minutes_between(session.planned_start, session.planned_end))
    target_day = day or timezone.localtime(session.planned_start).date()

    if start_time is not None:
        new_start = timezone.make_aware(datetime.combine(target_day, start_time))
        new_end = (
            timezone.make_aware(datetime.combine(end_day or target_day, end_time))
            if end_time is not None
            else new_start + duration
        )
        if split_stage and end_time is None:
            new_end = ProductionPlanningService._fit(calendar, new_start, int(duration.total_seconds() // 60))[1]
        company.assert_within_working_hours(new_start, new_end)
        PlannedProductionSession.assert_no_conflict(
            primary_resource_id=resource.id, start=new_start, end=new_end, exclude_pk=session.pk,
        )
    else:
        slot = find_free_slot_in_day(
            company=company, primary_resource_id=resource.id, day=target_day,
            duration=duration, exclude_pk=session.pk, resource_ids=resource_ids,
            ticket_id=session.production_ticket_id, stage=session.production_stage,
        )
        if slot is None:
            raise PlannedSessionConflictError(0, NO_FREE_SLOT_MESSAGE)
        new_start, new_end = slot

    candidate = SessionCandidate.from_session(session, resource_ids=resource_ids)
    candidate.company_id = company.pk
    candidate.start, candidate.end = new_start, new_end
    conflicts = ConflictChecker(company).check(candidate)
    rejected = [c for c in conflicts if c.is_blocking or c.code == ConflictCode.RESOURCE_OVERLAP]
    if rejected:
        raise PlannedSessionConflictError(session.pk, rejected[0].message)
    if session.company_id is None:
        session.company = company
    session.planned_start = new_start
    session.planned_end = new_end
    session.save(
        update_fields=[
            "company",
            "primary_resource",
            "planned_start",
            "planned_end",
            "updated_at",
        ]
    )
    sync_session_resources(session, resource_ids)
    return session


@transaction.atomic
def create_session(
    *, company, ticket, stage: str, employee, day: date,
    start_time: time | None = None, end_time: time | None = None, end_day: date | None = None,
) -> PlannedProductionSession:
    """Create a planned session from a backlog stage; auto-snaps to a free slot if start_time is
    omitted. *end_day* lets the session span more than one calendar day — defaults to *day*
    (today's single-day behavior) when omitted."""
    # Match the calendar's lock order: ticket before resources and bookings.
    ProductionTicket.objects.select_for_update().filter(pk=ticket.pk).exists()
    resource = get_or_create_resource_for_target(employee)
    ProductionPlanningService.lock_resources([resource.pk])

    if start_time is None:
        slot = find_free_slot_in_day(
            company=company, primary_resource_id=resource.id, day=day,
            duration=DEFAULT_SESSION_DURATION,
        )
        if slot is None:
            raise PlannedSessionConflictError(0, NO_FREE_SLOT_MESSAGE)
        new_start, new_end = slot
    else:
        new_start = timezone.make_aware(datetime.combine(day, start_time))
        new_end = (
            timezone.make_aware(datetime.combine(end_day or day, end_time))
            if end_time is not None
            else new_start + DEFAULT_SESSION_DURATION
        )
        if new_end <= new_start:
            raise ValueError(_("End time must be after start time."))
        company.assert_within_working_hours(new_start, new_end)
        PlannedProductionSession.assert_no_conflict(
            primary_resource_id=resource.id, start=new_start, end=new_end,
        )

    conflicts = ConflictChecker(company)._check_stage_part_overlap(SessionCandidate(
        company_id=company.pk, ticket_id=ticket.pk, stage=stage,
        start=new_start, end=new_end, resource_ids=[resource.pk],
    ), ())
    if conflicts:
        raise PlannedSessionConflictError(0, conflicts[0].message)
    session = PlannedProductionSession.objects.create(
        company=company,
        production_ticket=ticket,
        production_stage=stage,
        planned_start=new_start,
        planned_end=new_end,
        primary_resource=resource,
        locked=False,
        source=PlannedProductionSessionSource.MANUAL,
    )
    sync_session_resources(session, [resource.id])
    return session


def _pack_sequential(
    *, company, day: date, anchor: datetime, durations: list[timedelta],
) -> list[tuple[datetime, datetime]]:
    """Lay out *durations* back-to-back from *anchor*, spilling into the next working window as needed."""
    aware_windows = [
        (
            timezone.make_aware(datetime.combine(day, start_t)),
            timezone.make_aware(datetime.combine(day, end_t)),
        )
        for start_t, end_t in company.working_windows_for_day(day)
    ]

    win_idx = 0
    while win_idx < len(aware_windows) and anchor >= aware_windows[win_idx][1]:
        win_idx += 1
    if win_idx >= len(aware_windows):
        raise ValueError(_("No working hours left that day to reorder these tasks."))
    cursor = max(anchor, aware_windows[win_idx][0])

    slots: list[tuple[datetime, datetime]] = []
    for duration in durations:
        while win_idx < len(aware_windows) and cursor + duration > aware_windows[win_idx][1]:
            win_idx += 1
            if win_idx >= len(aware_windows):
                raise ValueError(_("Not enough working hours left that day to reorder all these tasks."))
            cursor = aware_windows[win_idx][0]
        slot_end = cursor + duration
        slots.append((cursor, slot_end))
        cursor = slot_end
    return slots


@transaction.atomic
def reorder_day_sessions(
    *, company, resource, day: date, session_ids: list[int],
) -> list[PlannedProductionSession]:
    """Re-sequence a resource's sessions for one day; *session_ids* must be its full set for that day."""
    if len(set(session_ids)) != len(session_ids):
        raise ValueError(_("The session list has duplicate ids."))

    day_start = timezone.make_aware(datetime.combine(day, time.min))
    day_end = day_start + timedelta(days=1)
    day_sessions = list(PlannedProductionSession.objects.for_employee(resource.target).filter(
        planned_start__gte=day_start, planned_start__lt=day_end,
    ))
    ticket_ids = {session.production_ticket_id for session in day_sessions if session.production_ticket_id}
    list(ProductionTicket.objects.select_for_update().filter(pk__in=ticket_ids).order_by("pk"))
    day_sessions = list(PlannedProductionSession.objects.select_for_update().filter(
        pk__in=[session.pk for session in day_sessions],
    ).prefetch_related("resources"))
    sessions_by_id = {s.id: s for s in day_sessions}

    if set(session_ids) != set(sessions_by_id):
        raise ValueError(_("The list doesn't match this employee's planned sessions for that day."))
    if any(s.locked for s in day_sessions):
        raise ValueError(_("One of the sessions is locked and can't be reordered."))

    ordered = [sessions_by_id[sid] for sid in session_ids]
    anchor = min(s.planned_start for s in ordered)
    durations = [s.planned_end - s.planned_start for s in ordered]
    slots = _pack_sequential(company=company, day=day, anchor=anchor, durations=durations)

    original_slots = {s.pk: (
        s.production_ticket_id, s.production_stage, s.planned_start, s.planned_end,
    ) for s in ordered}
    for session, (start, end) in zip(ordered, slots):
        session.planned_start = start
        session.planned_end = end
    ProductionPlanningService.lock_resources({
        assignment.resource_id for session in ordered for assignment in session.resources.all()
    })
    ProductionPlanningService._save_rescheduled(
        ordered, fields=["planned_start", "planned_end"], original_slots=original_slots,
    )
    checker = ConflictChecker(company)
    for session in ordered:
        candidate = SessionCandidate.from_session(session)
        candidate.company_id = company.pk
        conflicts = checker.check(candidate)
        rejected = [c for c in conflicts if c.is_blocking or c.code == ConflictCode.RESOURCE_OVERLAP]
        if rejected:
            raise ValueError(rejected[0].message)
    return ordered
