"""Move/create/reorder orchestration for the weekly planning board — spans
PlannedProductionSession, Company and ProductionResource."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from production.exceptions import PlannedSessionConflictError
from production.models import PlannedProductionSession, PlannedProductionSessionSource
from production.services.session_scheduler import get_or_create_resource_for_target

# Nominal duration when a backlog drop doesn't specify a time.
DEFAULT_SESSION_DURATION = timedelta(minutes=60)

NO_FREE_SLOT_MESSAGE = _("No free slot found that day for this employee.")


def find_free_slot_in_day(
    *, company, primary_resource_id: int, day: date, duration: timedelta,
    exclude_pk: int | None = None,
) -> tuple[datetime, datetime] | None:
    """Earliest free [start, end) for *primary_resource_id* on *day*, or None if nothing fits."""
    for start_t, end_t in company.working_windows_for_day(day):
        cursor = timezone.make_aware(datetime.combine(day, start_t))
        window_end = timezone.make_aware(datetime.combine(day, end_t))
        while cursor + duration <= window_end:
            blocker = (
                PlannedProductionSession.conflicts_for(
                    primary_resource_id=primary_resource_id,
                    start=cursor,
                    end=cursor + duration,
                    exclude_pk=exclude_pk,
                )
                .order_by("planned_end")
                .first()
            )
            if blocker is None:
                return cursor, cursor + duration
            cursor = blocker.planned_end
    return None


def move_session(
    *, company, session: PlannedProductionSession, employee, day: date | None,
    start_time: time | None = None, end_time: time | None = None,
) -> PlannedProductionSession:
    """Move/reassign a planned session; auto-snaps to a free slot if start_time is omitted."""
    if session.locked:
        raise ValueError(_("This session is locked and can't be moved."))

    resource = get_or_create_resource_for_target(employee) if employee else session.primary_resource
    duration = session.planned_end - session.planned_start
    target_day = day or timezone.localtime(session.planned_start).date()

    if start_time is not None:
        new_start = timezone.make_aware(datetime.combine(target_day, start_time))
        new_end = (
            timezone.make_aware(datetime.combine(target_day, end_time))
            if end_time is not None
            else new_start + duration
        )
        company.assert_within_working_hours(new_start, new_end)
        PlannedProductionSession.assert_no_conflict(
            primary_resource_id=resource.id, start=new_start, end=new_end, exclude_pk=session.pk,
        )
    else:
        slot = find_free_slot_in_day(
            company=company, primary_resource_id=resource.id, day=target_day,
            duration=duration, exclude_pk=session.pk,
        )
        if slot is None:
            raise PlannedSessionConflictError(0, NO_FREE_SLOT_MESSAGE)
        new_start, new_end = slot

    session.primary_resource = resource
    session.planned_start = new_start
    session.planned_end = new_end
    session.save(update_fields=["primary_resource", "planned_start", "planned_end"])
    return session


def create_session(
    *, company, ticket, stage: str, employee, day: date,
    start_time: time | None = None, end_time: time | None = None,
) -> PlannedProductionSession:
    """Create a planned session from a backlog stage; auto-snaps to a free slot if start_time is omitted."""
    resource = get_or_create_resource_for_target(employee)

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
            timezone.make_aware(datetime.combine(day, end_time))
            if end_time is not None
            else new_start + DEFAULT_SESSION_DURATION
        )
        if new_end <= new_start:
            raise ValueError(_("End time must be after start time."))
        company.assert_within_working_hours(new_start, new_end)
        PlannedProductionSession.assert_no_conflict(
            primary_resource_id=resource.id, start=new_start, end=new_end,
        )

    return PlannedProductionSession.objects.create(
        production_ticket=ticket,
        production_stage=stage,
        planned_start=new_start,
        planned_end=new_end,
        primary_resource=resource,
        locked=False,
        source=PlannedProductionSessionSource.MANUAL,
    )


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


def reorder_day_sessions(
    *, company, resource, day: date, session_ids: list[int],
) -> list[PlannedProductionSession]:
    """Re-sequence a resource's sessions for one day; *session_ids* must be its full set for that day."""
    if len(set(session_ids)) != len(session_ids):
        raise ValueError(_("The session list has duplicate ids."))

    day_start = timezone.make_aware(datetime.combine(day, time.min))
    day_end = day_start + timedelta(days=1)
    day_sessions = list(
        PlannedProductionSession.objects.filter(
            primary_resource=resource, planned_start__gte=day_start, planned_start__lt=day_end,
        )
    )
    sessions_by_id = {s.id: s for s in day_sessions}

    if set(session_ids) != set(sessions_by_id):
        raise ValueError(_("The list doesn't match this employee's planned sessions for that day."))
    if any(s.locked for s in day_sessions):
        raise ValueError(_("One of the sessions is locked and can't be reordered."))

    ordered = [sessions_by_id[sid] for sid in session_ids]
    anchor = min(s.planned_start for s in ordered)
    durations = [s.planned_end - s.planned_start for s in ordered]
    slots = _pack_sequential(company=company, day=day, anchor=anchor, durations=durations)

    for session, (start, end) in zip(ordered, slots):
        session.planned_start = start
        session.planned_end = end
    PlannedProductionSession.objects.bulk_update(ordered, ["planned_start", "planned_end"])
    return ordered
