from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta

from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext as _

from company.models import Employee, Machine
from production.models import (
    PlannedProductionSession,
    PlannedProductionSessionSource,
    ProductionResource,
    ProductionTicket,
)
from production.services.planning_conflicts import (
    Conflict,
    ConflictChecker,
    ConflictCode,
    SessionCandidate,
    blocking,
)
from production.services.planning_status_sync import sync_configuration_planning_status
from production.services.session_resources import (
    machine_content_type_id,
    sync_session_resources,
)
from production.services.working_calendar import WorkingCalendar


@dataclass
class MutationResult:
    ok: bool
    sessions: list = field(default_factory=list)
    conflicts: list[Conflict] = field(default_factory=list)
    requires_confirmation: bool = False

    @property
    def blocked(self) -> bool:
        return bool(blocking(self.conflicts))


class ProductionPlanningService:
    """The single write path for planned production sessions."""

    # --- helpers ---------------------------------------------------------

    @staticmethod
    def _checker(company, calendar=None) -> ConflictChecker:
        return ConflictChecker(company, calendar=calendar)

    @classmethod
    def _decide(cls, conflicts, override: bool) -> tuple[bool, bool]:
        """(may_apply, requires_confirmation) for a set of conflicts."""
        if blocking(conflicts):
            return False, False
        if conflicts and not override:
            return False, True
        return True, False

    @staticmethod
    def _locked(session) -> Conflict | None:
        candidate = SessionCandidate.from_session(session)
        if not candidate.locked:
            return None
        return ConflictChecker(session.company)._check_locked(candidate)[0]

    @staticmethod
    def _unknown_resources(company, resource_ids) -> bool:
        """Whether any id is missing, inactive, or another company's.

        The conflict checker silently drops ids it cannot load, so an unknown
        one would otherwise reach the database as a foreign key violation.
        """
        known = set(
            ProductionResource.objects.filter(
                pk__in=resource_ids, company=company, is_active=True
            ).values_list("pk", flat=True)
        )
        return set(resource_ids) != known

    @staticmethod
    def lock_resources(resource_ids):
        """Serialize capacity checks and writes for shared planning resources."""
        return list(
            ProductionResource.objects.select_for_update()
            .filter(pk__in=resource_ids)
            .order_by("pk")
            .values_list("pk", flat=True)
        )

    # --- single-session mutations ----------------------------------------

    @classmethod
    @transaction.atomic
    def move_session(
        cls,
        session_id: int,
        *,
        new_start: datetime,
        duration_minutes: int | None = None,
        resource_ids=None,
        override: bool = False,
    ) -> MutationResult:
        """Move a session, keeping its length unless *duration_minutes* is given."""
        session = cls._lock(session_id)
        if session is None:
            return cls._missing()

        minutes = duration_minutes or session.duration_minutes
        candidate = SessionCandidate.from_session(session)
        candidate.start = new_start
        candidate.end = new_start + timedelta(minutes=minutes)
        if resource_ids is not None:
            candidate.resource_ids = [int(r) for r in resource_ids]
            if cls._unknown_resources(session.company, candidate.resource_ids):
                return cls._problem(
                    "invalid_resources",
                    _("A selected resource is unavailable or belongs to another company."),
                )

        return cls._apply(session, candidate, override=override)

    @classmethod
    @transaction.atomic
    def resize_session(
        cls,
        session_id: int,
        *,
        new_start: datetime | None = None,
        new_end: datetime | None = None,
        override: bool = False,
    ) -> MutationResult:
        session = cls._lock(session_id)
        if session is None:
            return cls._missing()

        candidate = SessionCandidate.from_session(session)
        candidate.start = new_start or session.planned_start
        candidate.end = new_end or session.planned_end

        return cls._apply(session, candidate, override=override)

    @classmethod
    @transaction.atomic
    def set_resources(
        cls, session_id: int, *, resource_ids, override: bool = False
    ) -> MutationResult:
        session = cls._lock(session_id)
        if session is None:
            return cls._missing()

        candidate = SessionCandidate.from_session(session)
        candidate.resource_ids = [int(r) for r in resource_ids]
        if cls._unknown_resources(session.company, candidate.resource_ids):
            return cls._problem(
                "invalid_resources",
                _("A selected resource is unavailable or belongs to another company."),
            )

        return cls._apply(session, candidate, override=override)

    @classmethod
    @transaction.atomic
    def unplan(cls, session_id: int, *, override: bool = False) -> MutationResult:
        session = cls._lock(session_id)
        if session is None:
            return cls._missing()

        locked = cls._locked(session)
        if locked and not override:
            return MutationResult(ok=False, conflicts=[locked])

        # .delete() so post_delete fires and planning_status resets.
        session.delete()
        return MutationResult(ok=True)

    @classmethod
    @transaction.atomic
    def unplan_ticket(cls, ticket_id: int) -> MutationResult:
        sessions = list(
            PlannedProductionSession.objects.filter(
                production_ticket_id=ticket_id, locked=False
            )
        )
        for session in sessions:
            session.delete()
        return MutationResult(ok=True)

    # --- ticket-level ----------------------------------------------------

    @classmethod
    @transaction.atomic
    def plan_ticket(
        cls,
        ticket_id: int,
        *,
        start_at: datetime,
        anchor_resource_id: int | None = None,
        preferred_resource_ids: list[int] | None = None,
        override: bool = False,
    ) -> MutationResult:
        """Lay every stage of a ticket end to end from *start_at*, picking resources
        from the same pools the auto-scheduler uses."""
        from production.services.planning_stages import ticket_stage_plan

        ticket = (
            ProductionTicket.objects.select_related("company")
            .filter(pk=ticket_id)
            .first()
        )
        if ticket is None:
            return cls._missing()

        stages = ticket_stage_plan(ticket)
        if not stages:
            return MutationResult(
                ok=False,
                conflicts=[
                    Conflict(
                        code="no_stages",
                        severity="block",
                        message=_(
                            "This ticket has no stages with a configured duration."
                        ),
                    )
                ],
            )

        calendar = WorkingCalendar.for_company(ticket.company)
        checker = cls._checker(ticket.company, calendar)

        cursor = calendar.next_open(start_at) or start_at
        planned: list[tuple[SessionCandidate, dict]] = []
        conflicts: list[Conflict] = []
        preferred_ids = [
            int(resource_id) for resource_id in (preferred_resource_ids or [])
        ]
        if anchor_resource_id and int(anchor_resource_id) not in preferred_ids:
            preferred_ids.append(int(anchor_resource_id))
        preferred_used: set[int] = set()

        for sequence, stage in enumerate(stages):
            resource_ids, used_here = cls._pick_resources(
                stage, preferred_ids, preferred_used
            )
            preferred_used.update(used_here)

            start, end = cls._fit(calendar, cursor, stage["total_time_min"])

            candidate = SessionCandidate(
                company_id=ticket.company_id,
                stage=stage["stage"],
                start=start,
                end=end,
                resource_ids=resource_ids,
                ticket_id=ticket.pk,
                sequence=sequence,
            )
            planned.append((candidate, stage))
            cursor = end

        cls.lock_resources({
            resource_id for candidate, _ in planned for resource_id in candidate.resource_ids
        })
        for candidate, stage in planned:
            conflicts += checker.check(candidate)
            conflicts += checker.check_requirements(
                candidate,
                machine_qty=stage["machine_qty"],
                employee_qty=stage["employee_qty"],
            )

        may_apply, needs_confirm = cls._decide(conflicts, override)
        if not may_apply:
            return MutationResult(
                ok=False, conflicts=conflicts, requires_confirmation=needs_confirm
            )

        sessions = []
        for candidate, _stage in planned:
            session = PlannedProductionSession.objects.create(
                production_ticket=ticket,
                company=ticket.company,
                production_stage=candidate.stage,
                sequence=candidate.sequence,
                planned_start=candidate.start,
                planned_end=candidate.end,
                source=PlannedProductionSessionSource.MANUAL,
            )
            sync_session_resources(session, candidate.resource_ids)
            sessions.append(session)

        return MutationResult(ok=True, sessions=sessions, conflicts=conflicts)

    @classmethod
    @transaction.atomic
    def plan_ticket_custom(
        cls,
        ticket_id: int,
        *,
        start_at: datetime,
        stages: list[dict],
        override: bool = False,
    ) -> MutationResult:
        """Plan every product stage with ticket-specific times and resources.

        Durations are stored only as each session's interval, never rewriting
        the product template or configuration snapshot future orders use.
        """
        from production.services.planning_stages import resolve_ticket_stages

        ticket = (
            ProductionTicket.objects.select_related("company")
            .filter(pk=ticket_id)
            .first()
        )
        if ticket is None:
            return cls._missing()

        definitions = resolve_ticket_stages(ticket, include_unconfigured=True)
        if not definitions:
            return cls._problem(
                "no_stages",
                _("This ticket's product has no production stages configured."),
            )

        if not isinstance(stages, list):
            return cls._problem(
                "invalid_stages", _("The stage plan must be a list.")
            )

        allowed_names = [definition["stage"] for definition in definitions]
        submitted_names = [
            item.get("stage") if isinstance(item, dict) else None for item in stages
        ]
        if (
            any(not isinstance(name, str) for name in submitted_names)
            or len(submitted_names) != len(allowed_names)
            or len(set(submitted_names)) != len(submitted_names)
            or set(submitted_names) != set(allowed_names)
        ):
            return cls._problem(
                "invalid_stages",
                _("Include every product stage exactly once."),
            )

        definitions_by_name = {
            definition["stage"]: definition for definition in definitions
        }
        normalized: list[tuple[dict, int, list[int]]] = []
        all_resource_ids: set[int] = set()

        for item in stages:
            try:
                duration = int(item.get("duration_minutes"))
            except (TypeError, ValueError):
                duration = 0
            if duration <= 0:
                return cls._problem(
                    "invalid_duration",
                    _("Every stage needs a duration greater than zero minutes."),
                )

            raw_resource_ids = item.get("resource_ids", [])
            if not isinstance(raw_resource_ids, list):
                return cls._problem(
                    "invalid_resources", _("Stage resources must be a list.")
                )
            try:
                resource_ids = list(dict.fromkeys(int(pk) for pk in raw_resource_ids))
            except (TypeError, ValueError):
                return cls._problem(
                    "invalid_resources", _("A selected resource is invalid.")
                )
            if not resource_ids:
                return cls._problem(
                    "missing_resources",
                    _("Assign at least one machine or operator to every stage."),
                )

            all_resource_ids.update(resource_ids)
            normalized.append(
                (definitions_by_name[item["stage"]], duration, resource_ids)
            )

        valid_resource_ids = set(
            ProductionResource.objects.filter(
                pk__in=all_resource_ids,
                company=ticket.company,
                is_active=True,
            ).values_list("pk", flat=True)
        )
        if all_resource_ids != valid_resource_ids:
            return cls._problem(
                "invalid_resources",
                _("A selected resource is unavailable or belongs to another company."),
            )

        cls.lock_resources(all_resource_ids)

        calendar = WorkingCalendar.for_company(ticket.company)
        checker = cls._checker(ticket.company, calendar)
        cursor = calendar.next_open(start_at) or start_at
        planned: list[SessionCandidate] = []
        conflicts: list[Conflict] = []

        for sequence, (definition, duration, resource_ids) in enumerate(normalized):
            start, end = cls._fit(calendar, cursor, duration)
            candidate = SessionCandidate(
                company_id=ticket.company_id,
                stage=definition["stage"],
                start=start,
                end=end,
                resource_ids=resource_ids,
                ticket_id=ticket.pk,
                sequence=sequence,
            )
            conflicts += checker.check(candidate)
            conflicts += checker.check_requirements(
                candidate,
                machine_qty=definition["machine_qty"],
                employee_qty=definition["employee_qty"],
            )
            planned.append(candidate)
            cursor = end

        may_apply, needs_confirm = cls._decide(conflicts, override)
        if not may_apply:
            return MutationResult(
                ok=False, conflicts=conflicts, requires_confirmation=needs_confirm
            )

        sessions = []
        for candidate in planned:
            session = PlannedProductionSession.objects.create(
                production_ticket=ticket,
                company=ticket.company,
                production_stage=candidate.stage,
                sequence=candidate.sequence,
                planned_start=candidate.start,
                planned_end=candidate.end,
                source=PlannedProductionSessionSource.MANUAL,
            )
            sync_session_resources(session, candidate.resource_ids)
            sessions.append(session)

        return MutationResult(ok=True, sessions=sessions, conflicts=conflicts)

    @classmethod
    @transaction.atomic
    def plan_ticket_stage_custom(
        cls,
        ticket_id: int,
        *,
        start_at: datetime,
        stage: dict,
        assign_machines: bool = False,
        override: bool = False,
    ) -> MutationResult:
        """Plan one previously-unplanned product stage at a chosen time.

        Stages can be placed on different days in any order; sequence stays
        the product's fixed order and dropping one never moves existing work.
        An operator-only drop can request automatic machine assignment.
        """
        from production.services.planning_stages import resolve_ticket_stages

        ticket = (
            ProductionTicket.objects.select_related("company")
            .filter(pk=ticket_id)
            .first()
        )
        if ticket is None:
            return cls._missing()

        definitions = resolve_ticket_stages(ticket, include_unconfigured=True)
        if not definitions:
            return cls._problem(
                "no_stages",
                _("This ticket's product has no production stages configured."),
            )
        if not isinstance(stage, dict) or not isinstance(stage.get("stage"), str):
            return cls._problem("invalid_stage", _("Choose a valid production stage."))

        definitions_by_name = {definition["stage"]: definition for definition in definitions}
        stage_name = stage["stage"]
        definition = definitions_by_name.get(stage_name)
        if definition is None:
            return cls._problem("invalid_stage", _("Choose a valid production stage."))

        allowed_names = list(definitions_by_name)

        try:
            duration = int(stage.get("duration_minutes"))
        except (TypeError, ValueError):
            duration = 0
        if duration <= 0:
            return cls._problem(
                "invalid_duration", _("The stage needs a duration greater than zero minutes."),
            )

        raw_resource_ids = stage.get("resource_ids", [])
        if not isinstance(raw_resource_ids, list):
            return cls._problem("invalid_resources", _("Stage resources must be a list."))
        try:
            resource_ids = list(dict.fromkeys(int(pk) for pk in raw_resource_ids))
        except (TypeError, ValueError):
            return cls._problem("invalid_resources", _("A selected resource is invalid."))
        if not resource_ids:
            return cls._problem(
                "missing_resources", _("Assign at least one machine or operator to this stage."),
            )

        valid_resource_ids = set(
            ProductionResource.objects.filter(
                pk__in=resource_ids,
                company=ticket.company,
                is_active=True,
            ).values_list("pk", flat=True)
        )
        if set(resource_ids) != valid_resource_ids:
            return cls._problem(
                "invalid_resources",
                _("A selected resource is unavailable or belongs to another company."),
            )

        existing_sessions = list(
            PlannedProductionSession.objects.select_for_update(of=("self",)).filter(
                production_ticket=ticket
            )
        )
        if any(session.production_stage == stage_name for session in existing_sessions):
            return cls._problem(
                "stage_already_planned",
                _("This stage is already on the calendar. Reload the board."),
            )

        calendar = WorkingCalendar.for_company(ticket.company)
        checker = cls._checker(ticket.company, calendar)
        machine_ids = []
        operator_drop = (
            assign_machines
            and len(resource_ids) == 1
            and isinstance(checker.resources(resource_ids)[resource_ids[0]].target, Employee)
        )
        machine_qty = definition["machine_qty"]
        if operator_drop:
            machines = Machine.objects.filter(
                company=ticket.company,
                production_stages__contains=[stage_name],
            )
            if not definition["resource_requirements_configured"] and machines.exists():
                machine_qty = 1
            if machine_qty:
                machine_ids = list(
                    ProductionResource.objects.filter(
                        company=ticket.company,
                        is_active=True,
                        content_type_id=machine_content_type_id(),
                        object_id__in=machines.filter(is_active=True).values("pk"),
                    ).order_by("pk").values_list("pk", flat=True)
                )

        # Lock the entire candidate pool so concurrent drops choose from fresh capacity.
        cls.lock_resources([*resource_ids, *machine_ids])
        start, end = cls._fit(calendar, calendar.next_open(start_at) or start_at, duration)
        candidate = SessionCandidate(
            company_id=ticket.company_id,
            stage=stage_name,
            start=start,
            end=end,
            resource_ids=resource_ids,
            ticket_id=ticket.pk,
            sequence=allowed_names.index(stage_name),
        )
        if operator_drop and machine_qty:
            ranked_machine_ids = checker.resource_ids_by_capacity(
                replace(candidate, resource_ids=machine_ids)
            )
            candidate.resource_ids.extend(ranked_machine_ids[:machine_qty])
        conflicts = checker.check(candidate, require_explicit_stage=True)
        conflicts += checker.check_requirements(
            candidate,
            machine_qty=machine_qty,
            employee_qty=definition["employee_qty"],
        )
        # Operator drops may confirm missing machines; skills and operator counts stay strict.
        conflicts = [
            replace(conflict, severity="block")
            if conflict.code == ConflictCode.RESOURCE_NOT_SKILLED or (
                conflict.code == ConflictCode.REQUIREMENT_UNMET
                and (not operator_drop or conflict.detail.get("kind") != "machine")
            )
            else conflict
            for conflict in conflicts
        ]
        may_apply, needs_confirm = cls._decide(conflicts, override)
        if not may_apply:
            return MutationResult(
                ok=False, conflicts=conflicts, requires_confirmation=needs_confirm
            )

        session = PlannedProductionSession.objects.create(
            production_ticket=ticket,
            company=ticket.company,
            production_stage=candidate.stage,
            sequence=candidate.sequence,
            planned_start=candidate.start,
            planned_end=candidate.end,
            source=PlannedProductionSessionSource.MANUAL,
        )
        sync_session_resources(session, candidate.resource_ids)
        return MutationResult(ok=True, sessions=[session], conflicts=conflicts)

    @classmethod
    @transaction.atomic
    def reorder(
        cls,
        ticket_id: int,
        *,
        session_id: int,
        before_session_id: int | None = None,
        after_session_id: int | None = None,
        cascade: bool = True,
        override: bool = False,
    ) -> MutationResult:
        """Move one stage within the *complete* order of a production ticket.

        A source plus an adjacent stage is enough to rebuild the full order,
        since a ticket can span more days than the calendar currently shows.
        """
        if before_session_id and after_session_id:
            return cls._problem(
                "invalid_stage_order",
                _("Choose either the stage before or after the drop, not both."),
            )

        ticket = (
            ProductionTicket.objects.select_for_update(of=("self",))
            .select_related("company")
            .filter(pk=ticket_id)
            .first()
        )
        if ticket is None:
            return cls._missing()

        sessions = list(
            PlannedProductionSession.objects.select_for_update(of=("self",))
            .select_related("company")
            .prefetch_related("resources")
            .filter(production_ticket=ticket)
        )
        by_id = {session.pk: session for session in sessions}
        moved = by_id.get(session_id)
        neighbour_id = before_session_id or after_session_id
        neighbour = by_id.get(neighbour_id) if neighbour_id else None
        if moved is None or (neighbour_id is not None and neighbour is None):
            return cls._problem(
                "invalid_stage_order", _("The selected stages do not belong to this ticket.")
            )
        if neighbour is moved:
            return cls._problem(
                "invalid_stage_order", _("A stage cannot be placed next to itself.")
            )

        ordered = sorted(sessions, key=lambda session: (session.sequence, session.planned_start, session.pk))
        ordered.remove(moved)
        if neighbour:
            neighbour_index = ordered.index(neighbour)
            insert_at = neighbour_index if before_session_id else neighbour_index + 1
        else:
            insert_at = len(ordered)
        ordered.insert(insert_at, moved)

        company = ticket.company
        calendar = WorkingCalendar.for_company(company)
        checker = cls._checker(company, calendar)

        candidates = []
        conflicts: list[Conflict] = []
        # Re-flow from the ticket's current start, not the new first session's,
        # or reordering would drag the whole block later each time.
        anchor = min(session.planned_start for session in sessions)
        cursor = calendar.next_open(anchor) or anchor

        for sequence, session in enumerate(ordered):
            candidate = SessionCandidate.from_session(session)
            candidate.sequence = sequence

            if cascade:
                start, end = cls._fit(calendar, cursor, session.duration_minutes)
                candidate.start, candidate.end = start, end
                cursor = end

            candidates.append((session, candidate))

        # A locked session may stay in the ticket, but reorder can't move it.
        for session, candidate in candidates:
            if session.locked and (
                session.sequence != candidate.sequence
                or session.planned_start != candidate.start
                or session.planned_end != candidate.end
            ):
                return MutationResult(ok=False, conflicts=[cls._locked(session)])

        # Check only other tickets; this ticket's own old positions are transient.
        session_ids = set(by_id)
        cls.lock_resources({
            resource_id for _, candidate in candidates for resource_id in candidate.resource_ids
        })
        for session, candidate in candidates:
            if not session.locked:
                conflicts += checker.check(
                    candidate, exclude_session_ids=session_ids
                )

        may_apply, needs_confirm = cls._decide(conflicts, override)
        if not may_apply:
            return MutationResult(
                ok=False, conflicts=conflicts, requires_confirmation=needs_confirm
            )

        changed_sessions = []
        updated_at = timezone.now()
        for session, candidate in candidates:
            changed = False
            for field, value in (
                ("sequence", candidate.sequence),
                ("planned_start", candidate.start),
                ("planned_end", candidate.end),
            ):
                if getattr(session, field) != value:
                    setattr(session, field, value)
                    changed = True
            if changed:
                session.updated_at = updated_at
                changed_sessions.append(session)

        if changed_sessions:
            PlannedProductionSession.objects.bulk_update(
                changed_sessions,
                ["sequence", "planned_start", "planned_end", "updated_at"],
            )
            # Session existence is unchanged; sync its derived status once.
            sync_configuration_planning_status(ticket)

        # Override for this ticket only; ProductTemplate stays the default elsewhere.
        planned_stage_names = [session.production_stage for session in ordered]
        ticket.stage_order = planned_stage_names + [
            stage for stage in ticket.get_stage_order() if stage not in planned_stage_names
        ]
        ticket.save(update_fields=["stage_order", "updated_at"])

        return MutationResult(
            ok=True, sessions=[s for s, _ in candidates], conflicts=conflicts
        )

    # --- internals -------------------------------------------------------

    @staticmethod
    def _lock(session_id: int):
        # of=("self",): company/production_ticket are nullable LEFT JOINs, unlockable.
        return (
            PlannedProductionSession.objects.select_for_update(of=("self",))
            .select_related("company", "production_ticket")
            .filter(pk=session_id)
            .first()
        )

    @staticmethod
    def _missing() -> MutationResult:
        return MutationResult(
            ok=False,
            conflicts=[
                Conflict(
                    code="not_found",
                    severity="block",
                    message=_("That planning no longer exists. Reload the board."),
                )
            ],
        )

    @staticmethod
    def _problem(code: str, message: str) -> MutationResult:
        return MutationResult(
            ok=False,
            conflicts=[Conflict(code=code, severity="block", message=message)],
        )

    @classmethod
    def _apply(cls, session, candidate, *, override: bool) -> MutationResult:
        cls.lock_resources(candidate.resource_ids)
        checker = cls._checker(session.company)
        conflicts = checker.check(candidate)

        may_apply, needs_confirm = cls._decide(conflicts, override)
        if not may_apply:
            return MutationResult(
                ok=False, conflicts=conflicts, requires_confirmation=needs_confirm
            )

        session.planned_start = candidate.start
        session.planned_end = candidate.end
        session.sequence = candidate.sequence
        session.source = PlannedProductionSessionSource.MANUAL
        session.save(
            update_fields=[
                "planned_start",
                "planned_end",
                "sequence",
                "source",
                "updated_at",
            ]
        )
        sync_session_resources(session, candidate.resource_ids)
        session.refresh_from_db()

        return MutationResult(ok=True, sessions=[session], conflicts=conflicts)

    @staticmethod
    def _fit(calendar: WorkingCalendar, cursor: datetime, minutes: int):
        """Place *minutes* of work from *cursor*, skipping closed time.

        The returned interval may span a break; the daily calendar shades that
        closed portion while retaining the session's real start and end times.
        """
        minutes = max(int(minutes or 0), 1)
        start = calendar.next_open(cursor) or cursor

        remaining = minutes
        end = start
        for window in calendar.windows_between(start, start + timedelta(days=365)):
            available = window.minutes
            if available <= 0:
                continue
            if available >= remaining:
                end = window.start + timedelta(minutes=remaining)
                remaining = 0
                break
            remaining -= available
            end = window.end

        if remaining:  # no schedule configured: fall back to wall clock
            end = start + timedelta(minutes=minutes)

        return start, end

    @staticmethod
    def _pick_resources(stage: dict, preferred_resource_ids, already_used):
        """Choose resources, preferring the calendar rows receiving the drop."""
        machines = list(stage["machine_pool"])
        employees = list(stage["employee_pool"])

        available_preferences = [
            resource_id
            for resource_id in preferred_resource_ids
            if resource_id not in already_used
        ]
        for pool in (machines, employees):
            pool.sort(
                key=lambda resource: (
                    available_preferences.index(resource.pk)
                    if resource.pk in available_preferences
                    else len(available_preferences),
                    resource.pk,
                )
            )

        chosen = [r.pk for r in machines[: stage["machine_qty"]]]
        chosen += [r.pk for r in employees[: stage["employee_qty"]]]
        return chosen, set(chosen) & set(available_preferences)
