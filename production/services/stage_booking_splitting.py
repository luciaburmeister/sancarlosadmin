"""Divide calendar bookings without changing production work."""

from dataclasses import replace
from datetime import datetime, time, timedelta

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext as _

from production.models import PlannedProductionSession, PlannedProductionSessionSource
from production.services.planning_conflicts import ConflictCode, SessionCandidate, blocking
from production.services.production_planning import MutationResult, ProductionPlanningService
from production.services.session_resources import sync_session_resources
from production.services.working_calendar import MAX_LOOKAHEAD_DAYS, WorkingCalendar


class StageBookingSplittingService:
    @classmethod
    def fit_overflow(
        cls, company, candidate, *, requested_start, minutes, session, timetable,
        override=False, resource_conflicts=(),
    ):
        """Offer daily portions, then revalidate the accepted plan under caller locks.

        Only timetable callers opt in. Existing app planning keeps its contract.
        None means that the booking fits and the normal mutation should proceed.
        """
        planning = ProductionPlanningService
        accepted = timetable.get("overflow_plan")
        if session and isinstance(accepted, dict) and (
            accepted.get("original_start") != session.planned_start.isoformat()
            or accepted.get("original_end") != session.planned_end.isoformat()
        ):
            return planning._problem("stale_booking", _("This booking has changed. Reload it before moving."))
        calendar = WorkingCalendar.for_company(company)
        if not calendar.has_slots or minutes <= 0:
            if accepted:
                return planning._problem(
                    "calendar_changed", _("Working hours have changed. Reload the calendar before planning again."),
                )
            return None
        day = timezone.localtime(requested_start, calendar.timezone).date()
        windows = calendar.windows_for_date(day)
        closing = windows[-1].end if windows else requested_start
        available = calendar.working_minutes_between(requested_start, closing)
        if minutes <= available:
            if accepted:
                return planning._problem(
                    "calendar_changed", _("Working hours have changed. Reload the calendar before planning again."),
                )
            return None
        if not candidate.ticket_id:
            return planning._problem("ticket_required", _("Only bookings linked to a production ticket can be split."))
        if session:
            ticket = session.production_ticket
            if ticket.company_id != company.pk:
                return planning._problem("cross_company", _("The booking and ticket must belong to the same company."))
            if ticket.items.exists() and not ticket.has_incomplete_stage(candidate.stage):
                return planning._problem("finished_stage", _("This stage is finished. Reopen it before planning more work."))
        if not candidate.resource_ids and session and session.primary_resource_id:
            candidate.resource_ids = [session.primary_resource_id]
        if planning._unknown_resources(company, candidate.resource_ids):
            return planning._problem("invalid_resources", _("A selected resource is unavailable or belongs to another company."))
        planning.lock_resources(candidate.resource_ids)
        checker = planning._checker(company, calendar)
        conflicts = list(resource_conflicts)
        portions = []
        remaining = minutes
        if available:
            start = calendar.next_open(requested_start)
            first = replace(candidate, start=start, end=closing)
            conflicts.extend(checker.check(first))
            portions.append(first)
            remaining -= available
        if candidate.locked:
            return MutationResult(ok=False, conflicts=[planning._locked(session)])
        if blocking(conflicts):
            return MutationResult(ok=False, conflicts=conflicts)

        # Booking ends are the only instants at which resource capacity can free up.
        # Load them once; no minute-by-minute database search is needed.
        horizon = day + timedelta(days=MAX_LOOKAHEAD_DAYS)
        boundary = windows[-1].end if windows else requested_start
        horizon_end = timezone.make_aware(datetime.combine(horizon + timedelta(days=1), time.min), calendar.timezone)
        search_start = timezone.make_aware(datetime.combine(day, time.min), calendar.timezone)
        intervals = checker.preload_availability(candidate.resource_ids, search_start, horizon_end)
        stage_bookings = list(PlannedProductionSession.objects.filter(
            production_ticket_id=candidate.ticket_id, production_stage=candidate.stage,
            planned_end__gt=boundary, planned_start__lt=horizon_end,
        ).exclude(pk=candidate.session_id).values_list("planned_start", "planned_end"))
        release_times = {row[3] for row in intervals if row[0] != candidate.session_id}
        release_times.update(end for start, end in stage_bookings)
        unavailable = {
            ConflictCode.RESOURCE_OVERLAP, ConflictCode.DAILY_CAPACITY_EXCEEDED,
            ConflictCode.STAGE_PART_OVERLAP, ConflictCode.DUPLICATE_STAGE_SLOT,
        }
        while remaining and day < horizon:
            day += timedelta(days=1)
            windows = calendar.windows_for_date(day)
            if not windows:
                continue
            starts = {w.start for w in windows}
            starts.update(
                moment for moment in release_times
                if windows[0].start <= moment < windows[-1].end
            )
            for cursor in sorted(starts):
                start = calendar.next_open(cursor)
                if start is None or start >= windows[-1].end:
                    continue
                count = min(remaining, calendar.working_minutes_between(start, windows[-1].end))
                if count < 1:
                    continue
                start, end = planning._fit(calendar, start, count)
                part = replace(candidate, start=start, end=end)
                if any(lo < end and hi > start for lo, hi in stage_bookings):
                    continue
                found_conflicts = checker.check_resource_availability(part)
                if any(c.code in unavailable for c in found_conflicts):
                    continue
                found_conflicts = checker.check(part)
                if blocking(found_conflicts):
                    return MutationResult(ok=False, conflicts=found_conflicts)
                portions.append(part)
                conflicts.extend(found_conflicts)
                remaining -= count
                break
        if remaining:
            return planning._problem(
                "no_available_slot",
                _("No available working slot was found within the next %(days)s days.")
                % {"days": MAX_LOOKAHEAD_DAYS},
            )
        plan = {
            "parts": [{"start": p.start.isoformat(), "end": p.end.isoformat(),
                       "minutes": calendar.working_minutes_between(p.start, p.end)} for p in portions],
            "resource_ids": sorted(candidate.resource_ids),
            "original_start": session.planned_start.isoformat() if session else None,
            "original_end": session.planned_end.isoformat() if session else None,
            "conflicts": [c.as_dict() for c in conflicts],
        }
        if timetable.get("overflow_plan") != plan:
            return MutationResult(ok=False, requires_confirmation=True, conflicts=conflicts, overflow_plan=plan)
        may_apply, needs_confirm = planning._decide(conflicts, override)
        if not may_apply:
            return MutationResult(ok=False, conflicts=conflicts, requires_confirmation=needs_confirm,
                                  overflow_plan=plan)
        saved = []
        for index, part in enumerate(portions):
            if index == 0 and session:
                booking = session
                booking.planned_start, booking.planned_end = part.start, part.end
                booking.source = PlannedProductionSessionSource.MANUAL
                booking.save(update_fields=["planned_start", "planned_end", "source", "updated_at"])
            else:
                booking = PlannedProductionSession.objects.create(
                    company=company, production_ticket_id=candidate.ticket_id,
                    production_stage=candidate.stage, sequence=candidate.sequence,
                    planned_start=part.start, planned_end=part.end,
                    notes=session.notes if session else "",
                    source=PlannedProductionSessionSource.MANUAL,
                )
            sync_session_resources(booking, candidate.resource_ids)
            saved.append(booking)
        return MutationResult(ok=True, sessions=saved, conflicts=conflicts)

    @classmethod
    @transaction.atomic
    def split_booking(cls, session_id, *, cut_at, expected_start, expected_end, override=False):
        session = ProductionPlanningService._lock(session_id)
        if session is None:
            return ProductionPlanningService._missing()
        if session.locked:
            return MutationResult(ok=False, conflicts=[ProductionPlanningService._locked(session)])
        if not session.production_ticket_id:
            raise ValidationError(_("Only bookings linked to a production ticket can be split."))
        ticket = session.production_ticket
        if not session.company_id or ticket.company_id != session.company_id:
            raise ValidationError(_("The booking and ticket must belong to the same company."))
        if (session.planned_start, session.planned_end) != (expected_start, expected_end):
            raise ValidationError(_("This booking has changed. Reload it before splitting."))
        if ticket.items.exists() and not ticket.has_incomplete_stage(session.production_stage):
            raise ValidationError(_("This stage is finished. Reopen it before planning more work."))
        if not session.planned_start < cut_at < session.planned_end:
            raise ValidationError(_("Choose a cut inside the booking, with at least one working minute on each side."))

        calendar = WorkingCalendar.for_company(session.company)
        original_end = session.planned_end
        continuation_start = calendar.next_open(cut_at) if calendar.has_slots else cut_at
        first_minutes = cls.working_minutes(session, calendar, end=cut_at)
        total_minutes = cls.working_minutes(session, calendar)
        if calendar.has_slots:
            remaining = calendar.working_minutes_between(cut_at, original_end)
            windows = calendar.windows_between(session.planned_start, original_end)
            valid_cut = any(window.start <= cut_at <= window.end for window in windows)
        else:
            remaining = int((original_end - cut_at).total_seconds() // 60)
            valid_cut = True
        if (
            continuation_start is None or not valid_cut or first_minutes < 1
            or remaining < 1 or first_minutes + remaining != total_minutes
        ):
            raise ValidationError(_("Choose a cut inside the booking, with at least one working minute on each side."))

        candidate = SessionCandidate.from_session(session)
        if not candidate.resource_ids and session.primary_resource_id:
            candidate.resource_ids = [session.primary_resource_id]
        if ProductionPlanningService._unknown_resources(session.company, candidate.resource_ids):
            return ProductionPlanningService._problem(
                "invalid_resources", _("A selected resource is unavailable or belongs to another company."),
            )
        ProductionPlanningService.lock_resources(candidate.resource_ids)
        checker = ProductionPlanningService._checker(session.company, calendar)
        conflicts = []
        saved = []
        for index, (start, end) in enumerate((
            (session.planned_start, cut_at), (continuation_start, original_end),
        )):
            candidate.start, candidate.end = start, end
            candidate.session_id = session.pk if index == 0 else None
            conflicts.extend(checker.check(candidate))
            may_apply, needs_confirm = ProductionPlanningService._decide(conflicts, override)
            if not may_apply:
                transaction.set_rollback(True)
                return MutationResult(ok=False, conflicts=conflicts, requires_confirmation=needs_confirm)
            if index == 0:
                session.planned_end = cut_at
                session.source = PlannedProductionSessionSource.MANUAL
                session.save(update_fields=["planned_end", "source", "updated_at"])
                saved.append(session)
            else:
                continuation = PlannedProductionSession.objects.create(
                    company=session.company, production_ticket=ticket,
                    production_stage=session.production_stage, sequence=session.sequence,
                    planned_start=start, planned_end=end, notes=session.notes,
                    source=PlannedProductionSessionSource.MANUAL,
                )
                sync_session_resources(continuation, candidate.resource_ids)
                saved.append(continuation)
        return MutationResult(ok=True, sessions=saved, conflicts=conflicts)

    @staticmethod
    def working_minutes(session, calendar=None, *, end=None):
        calendar = calendar or WorkingCalendar.for_company(session.company)
        end = end or session.planned_end
        if calendar.has_slots:
            return calendar.working_minutes_between(session.planned_start, end)
        return max(0, int((end - session.planned_start).total_seconds() // 60))
