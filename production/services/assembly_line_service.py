import unicodedata
import logging

from django.core.exceptions import ValidationError
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from production.models import (
    AssemblyLine,
    AssemblyLineStatus,
    ProductionItem,
    ProductionItemSession,
    ProductionItemSessionStatus,
    ProductionItemStatus,
    ProductionTicketStatus,
)
from production.services.stages_to_sessions import StagesToSessionsService
from company.models import Employee

logger = logging.getLogger(__name__)


class AssemblyLineService:
    """Orchestrates bulk start / pause / finish operations for an AssemblyLine.

    Stages can be processed **sequentially** (default) or **in parallel**
    (when ``assembly_line.parallel_stages`` is True).

    In sequential mode only the first incomplete stage can be started.
    In parallel mode all incomplete stages are started at once.

    Multiple workers can **join** a running line — each gets their own
    sessions on the active items.
    """

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @classmethod
    def get_current_stage(
        cls,
        assembly_line: AssemblyLine,
        tickets: list | None = None,
    ) -> str | None:
        """Return the first stage that is not fully done across all tickets.

        Returns None when every stage is complete.

        Pass a pre-fetched *tickets* list to avoid redundant DB queries.
        """
        stages = cls._normalize_stages(assembly_line)
        if tickets is None:
            tickets = list(cls._active_tickets(assembly_line))

        for stage in stages:
            for ticket in tickets:
                items = cls._items_for_stage(ticket, stage, target_statuses={
                    ProductionItemStatus.PENDING,
                    ProductionItemStatus.IN_PROGRESS,
                })
                if items:
                    return stage

        return None

    @classmethod
    def get_active_stages(
        cls,
        assembly_line: AssemblyLine,
        tickets: list | None = None,
    ) -> list[str]:
        """Return stages that are actually running (have IN_PROGRESS items).

        In sequential mode this returns at most one stage (same as
        ``get_current_stage``). In parallel mode it only returns stages
        where work has been started — not stages still fully PENDING.
        """
        if not assembly_line.parallel_stages:
            stage = cls.get_current_stage(assembly_line, tickets=tickets)
            return [stage] if stage else []

        stages = cls._normalize_stages(assembly_line)
        if tickets is None:
            tickets = list(cls._active_tickets(assembly_line))

        active: list[str] = []
        for stage in stages:
            for ticket in tickets:
                # Only IN_PROGRESS items — not PENDING (those haven't been started)
                items = cls._items_for_stage(ticket, stage, target_statuses={
                    ProductionItemStatus.IN_PROGRESS,
                })
                if items:
                    active.append(stage)
                    break  # stage confirmed active, move to next
        return active

    @classmethod
    def get_available_stages(
        cls,
        assembly_line: AssemblyLine,
        tickets: list | None = None,
    ) -> list[str]:
        """Return stages that still have work to do (PENDING or IN_PROGRESS).

        Used by the UI to know which stages to display banners for.
        """
        stages = cls._normalize_stages(assembly_line)
        if tickets is None:
            tickets = list(cls._active_tickets(assembly_line))

        available: list[str] = []
        for stage in stages:
            for ticket in tickets:
                items = cls._items_for_stage(ticket, stage, target_statuses={
                    ProductionItemStatus.PENDING,
                    ProductionItemStatus.IN_PROGRESS,
                })
                if items:
                    available.append(stage)
                    break
        return available

    @classmethod
    def start_line(cls, assembly_line: AssemblyLine, employee: Employee) -> dict:
        """Start the assembly line.

        Sequential mode: starts the current (first incomplete) stage.
        Parallel mode: only transitions the line to IN_PROGRESS — stages
        must be started individually via ``start_stage``.

        Returns a summary dict: {started: int, skipped: int, stages: [str], errors: [str]}
        """
        cls._assert_can_transition(
            assembly_line,
            allowed={AssemblyLineStatus.READY, AssemblyLineStatus.PAUSED, AssemblyLineStatus.DRAFT},
        )

        started = 0
        skipped = 0
        errors: list[str] = []
        started_stages: list[str] = []

        if assembly_line.parallel_stages:
            # Parallel: just transition the line status.
            # Individual stages are started via start_stage().
            pass
        else:
            # Sequential: start only the current stage.
            tickets = list(cls._active_tickets(assembly_line))
            active_stages = cls.get_active_stages(assembly_line, tickets=tickets)
            if not active_stages:
                # Update status even if nothing to start (e.g. resuming)
                pass
            else:
                started_stages = active_stages
                for stage in active_stages:
                    for ticket in tickets:
                        items = cls._items_for_stage(ticket, stage, target_statuses={
                            ProductionItemStatus.PENDING,
                            ProductionItemStatus.IN_PROGRESS,
                        })
                        if not items:
                            skipped += 1
                            continue

                        try:
                            count = StagesToSessionsService.start_activity(
                                stage_name=stage, items=items, employee=employee,
                                skip_worker_check=True,
                            )
                            started += count
                        except ValidationError as exc:
                            errors.append(f"Ticket #{ticket.pk} [{stage}]: {exc.message}")
                        except Exception as exc:
                            logger.exception("start_line error on ticket %s stage %s", ticket.pk, stage)
                            errors.append(f"Ticket #{ticket.pk} [{stage}]: {exc}")

        now = timezone.now()
        AssemblyLine.objects.filter(pk=assembly_line.pk).update(
            status=AssemblyLineStatus.IN_PROGRESS,
            started_by=employee,
            started_at=assembly_line.started_at or now,
        )
        assembly_line.status = AssemblyLineStatus.IN_PROGRESS
        assembly_line.started_by = employee
        assembly_line.started_at = assembly_line.started_at or now

        return {"started": started, "skipped": skipped, "stages": started_stages, "errors": errors}

    @classmethod
    def join_line(cls, assembly_line: AssemblyLine, employee: Employee) -> dict:
        """Join an already-running line — the employee gets sessions on all
        currently active stages.

        Returns a summary dict: {joined: int, skipped: int, stages: [str], errors: [str]}
        """
        cls._assert_can_transition(
            assembly_line,
            allowed={AssemblyLineStatus.IN_PROGRESS, AssemblyLineStatus.PAUSED},
        )

        tickets = list(cls._active_tickets(assembly_line))
        active_stages = cls.get_active_stages(assembly_line, tickets=tickets)
        if not active_stages:
            return {"joined": 0, "skipped": 0, "stages": [], "errors": []}

        joined = 0
        skipped = 0
        errors: list[str] = []

        for stage in active_stages:
            for ticket in tickets:
                items = cls._items_for_stage(ticket, stage, target_statuses={
                    ProductionItemStatus.IN_PROGRESS,
                })
                if not items:
                    skipped += 1
                    continue

                try:
                    count = StagesToSessionsService.start_group_activity(
                        stage_name=stage, items=items, employee=employee,
                        skip_worker_check=True,
                    )
                    joined += count
                except ValidationError as exc:
                    errors.append(f"Ticket #{ticket.pk} [{stage}]: {exc.message}")
                except Exception as exc:
                    logger.exception("join_line error on ticket %s stage %s", ticket.pk, stage)
                    errors.append(f"Ticket #{ticket.pk} [{stage}]: {exc}")

        return {"joined": joined, "skipped": skipped, "stages": active_stages, "errors": errors}

    @classmethod
    def start_stage(cls, assembly_line: AssemblyLine, employee: Employee, stage_name: str) -> dict:
        """Start a single specific stage on a line (parallel mode).

        Also transitions the line to IN_PROGRESS if it was paused/draft/ready.

        Returns a summary dict: {started: int, skipped: int, stage: str, errors: [str]}
        """
        cls._assert_can_transition(
            assembly_line,
            allowed={
                AssemblyLineStatus.IN_PROGRESS,
                AssemblyLineStatus.PAUSED,
                AssemblyLineStatus.READY,
                AssemblyLineStatus.DRAFT,
            },
        )

        import unicodedata
        stage_name = unicodedata.normalize("NFC", stage_name)

        tickets = list(cls._active_tickets(assembly_line))
        started = 0
        skipped = 0
        errors: list[str] = []

        for ticket in tickets:
            items = cls._items_for_stage(ticket, stage_name, target_statuses={
                ProductionItemStatus.PENDING,
                ProductionItemStatus.IN_PROGRESS,
            })
            if not items:
                skipped += 1
                continue

            try:
                count = StagesToSessionsService.start_activity(
                    stage_name=stage_name, items=items, employee=employee,
                    skip_worker_check=True,
                )
                started += count
            except ValidationError as exc:
                errors.append(f"Ticket #{ticket.pk} [{stage_name}]: {exc.message}")
            except Exception as exc:
                logger.exception("start_stage error on ticket %s stage %s", ticket.pk, stage_name)
                errors.append(f"Ticket #{ticket.pk} [{stage_name}]: {exc}")

        # Ensure the line is IN_PROGRESS
        if assembly_line.status != AssemblyLineStatus.IN_PROGRESS:
            now = timezone.now()
            AssemblyLine.objects.filter(pk=assembly_line.pk).update(
                status=AssemblyLineStatus.IN_PROGRESS,
                started_by=employee,
                started_at=assembly_line.started_at or now,
            )
            assembly_line.status = AssemblyLineStatus.IN_PROGRESS
            assembly_line.started_by = employee
            assembly_line.started_at = assembly_line.started_at or now

        return {"started": started, "skipped": skipped, "stage": stage_name, "errors": errors}

    @classmethod
    def pause_line(cls, assembly_line: AssemblyLine, employee: Employee) -> dict:
        """The calling employee leaves all active stages.

        If no other workers have active sessions remaining on the line,
        the line transitions to PAUSED.  Otherwise it stays IN_PROGRESS.

        Returns a summary dict: {paused: int, skipped: int, stages: [str],
                                  line_paused: bool, errors: [str]}
        """
        cls._assert_can_transition(
            assembly_line,
            allowed={AssemblyLineStatus.IN_PROGRESS, AssemblyLineStatus.PAUSED},
        )

        tickets = list(cls._active_tickets(assembly_line))
        active_stages = cls.get_active_stages(assembly_line, tickets=tickets)
        if not active_stages:
            return {"paused": 0, "skipped": 0, "stages": [], "line_paused": False, "errors": []}

        paused = 0
        skipped = 0
        errors: list[str] = []

        for stage in active_stages:
            for ticket in tickets:
                items = cls._items_for_stage(ticket, stage, target_statuses={
                    ProductionItemStatus.IN_PROGRESS,
                })
                if not items:
                    skipped += 1
                    continue

                try:
                    count = StagesToSessionsService.leave_activity(
                        stage_name=stage, items=items, employee=employee,
                    )
                    paused += count
                except ValidationError:
                    # Employee had no active session on this stage — not an error
                    pass
                except Exception as exc:
                    logger.exception("pause_line error on ticket %s stage %s", ticket.pk, stage)
                    errors.append(f"Ticket #{ticket.pk} [{stage}]: {exc}")

        # Check if ANY worker still has open sessions on this line's items
        line_paused = not cls._line_has_active_sessions(assembly_line)

        if line_paused:
            AssemblyLine.objects.filter(pk=assembly_line.pk).update(
                status=AssemblyLineStatus.PAUSED,
            )
            assembly_line.status = AssemblyLineStatus.PAUSED

        return {
            "paused": paused,
            "skipped": skipped,
            "stages": active_stages,
            "line_paused": line_paused,
            "errors": errors,
        }

    @classmethod
    def finish_line(cls, assembly_line: AssemblyLine, employee: Employee) -> dict:
        """Finish the active stage(s) for ALL in-progress items across ALL tickets.

        If all stages are now complete, marks the line as DONE.

        Returns a summary dict: {finished: int, skipped: int, stages: [str], errors: [str]}
        """
        cls._assert_can_transition(
            assembly_line,
            allowed={AssemblyLineStatus.IN_PROGRESS},
        )

        tickets = list(cls._active_tickets(assembly_line))
        active_stages = cls.get_active_stages(assembly_line, tickets=tickets)
        if not active_stages:
            return {"finished": 0, "skipped": 0, "stages": [], "errors": []}

        finished = 0
        skipped = 0
        errors: list[str] = []

        for stage in active_stages:
            for ticket in tickets:
                items = cls._items_for_stage(ticket, stage, target_statuses={
                    ProductionItemStatus.IN_PROGRESS,
                })
                if not items:
                    skipped += 1
                    continue

                try:
                    count = StagesToSessionsService.finish_activity(
                        stage_name=stage, items=items, employee=employee,
                    )
                    finished += count
                except ValidationError as exc:
                    errors.append(f"Ticket #{ticket.pk} [{stage}]: {exc.message}")
                except Exception as exc:
                    logger.exception("finish_line error on ticket %s stage %s", ticket.pk, stage)
                    errors.append(f"Ticket #{ticket.pk} [{stage}]: {exc}")

        # Re-fetch tickets with fresh item data (statuses mutated above)
        fresh_tickets = list(cls._active_tickets(assembly_line))
        remaining = cls.get_active_stages(assembly_line, tickets=fresh_tickets)

        if not remaining:
            now = timezone.now()
            AssemblyLine.objects.filter(pk=assembly_line.pk).update(
                status=AssemblyLineStatus.DONE,
                finished_at=now,
            )
            assembly_line.status = AssemblyLineStatus.DONE
            assembly_line.finished_at = now
        else:
            # Move back to READY so the user can start the next stage(s)
            AssemblyLine.objects.filter(pk=assembly_line.pk).update(
                status=AssemblyLineStatus.READY,
            )
            assembly_line.status = AssemblyLineStatus.READY

        return {"finished": finished, "skipped": skipped, "stages": active_stages, "errors": errors}

    @classmethod
    def get_line_progress(cls, assembly_line: AssemblyLine) -> dict:
        """Return aggregated progress for the assembly line with per-stage detail.

        Returns: {
            total_tickets, total_items, pending, in_progress, done,
            stages: [{name, pending, in_progress, done, is_current}],
            current_stage: str | None,
            active_stages: [str],
            tickets: [{id, name, ...}]
        }
        """
        stages = cls._normalize_stages(assembly_line)

        tickets_data = []
        total_items = 0
        total_pending = 0
        total_in_progress = 0
        total_done = 0

        # Per-stage aggregates
        stage_aggregates = {s: {"pending": 0, "in_progress": 0, "done": 0} for s in stages}

        # Single query: fetch all tickets with their items in one go
        all_tickets = list(
            assembly_line.tickets.select_related("company").prefetch_related("items__component")
        )

        # Determine current/active stages from only active (non-cancelled/done) tickets
        active_tickets = [
            t for t in all_tickets
            if t.status not in (ProductionTicketStatus.CANCELLED, ProductionTicketStatus.DONE)
        ]
        current_stage = cls.get_current_stage(assembly_line, tickets=active_tickets)
        active_stages = cls.get_active_stages(assembly_line, tickets=active_tickets)

        for ticket in all_tickets:
            items = list(ticket.items.all())
            t_pending = 0
            t_in_progress = 0
            t_done = 0

            for stage in stages:
                for item in items:
                    planning = {
                        unicodedata.normalize("NFC", k): v
                        for k, v in (item.planning or {}).items()
                    }
                    if stage not in planning:
                        continue

                    info = planning[stage] or {}
                    status = info.get("status") or ProductionItemStatus.PENDING

                    if status == ProductionItemStatus.DONE:
                        stage_aggregates[stage]["done"] += 1
                        t_done += 1
                    elif status == ProductionItemStatus.IN_PROGRESS:
                        stage_aggregates[stage]["in_progress"] += 1
                        t_in_progress += 1
                    else:
                        stage_aggregates[stage]["pending"] += 1
                        t_pending += 1

            total_items += t_pending + t_in_progress + t_done

            source = ticket.source_object
            ticket_name = getattr(source, "name", None) or str(source) if source else str(ticket)

            tickets_data.append({
                "id": ticket.pk,
                "name": ticket_name,
                "product": ticket.context.get("product", "—"),
                "pending": t_pending,
                "in_progress": t_in_progress,
                "done": t_done,
                "total": t_pending + t_in_progress + t_done,
            })

            total_pending += t_pending
            total_in_progress += t_in_progress
            total_done += t_done

        stages_detail = []
        for s in stages:
            agg = stage_aggregates[s]
            stages_detail.append({
                "name": s,
                "pending": agg["pending"],
                "in_progress": agg["in_progress"],
                "done": agg["done"],
                "total": agg["pending"] + agg["in_progress"] + agg["done"],
                "is_current": s == current_stage,
                "is_active": s in active_stages,
                "is_done": agg["pending"] == 0 and agg["in_progress"] == 0 and agg["done"] > 0,
            })

        return {
            "total_tickets": len(tickets_data),
            "total_items": total_items,
            "pending": total_pending,
            "in_progress": total_in_progress,
            "done": total_done,
            "stages": stages_detail,
            "current_stage": current_stage,
            "active_stages": active_stages,
            "tickets": tickets_data,
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @classmethod
    def _normalize_stages(cls, assembly_line: AssemblyLine) -> list[str]:
        return [
            unicodedata.normalize("NFC", s)
            for s in (assembly_line.stages or [])
        ]

    @classmethod
    def _assert_can_transition(cls, assembly_line: AssemblyLine, allowed: set):
        if assembly_line.status not in allowed:
            raise ValidationError(
                _("Cannot perform this action on a line with status '%(status)s'.")
                % {"status": assembly_line.get_status_display()}
            )

    @classmethod
    def _active_tickets(cls, assembly_line: AssemblyLine):
        """Return tickets that are not cancelled/done."""
        return assembly_line.tickets.exclude(
            status__in=[ProductionTicketStatus.CANCELLED, ProductionTicketStatus.DONE]
        ).prefetch_related("items__component")

    @classmethod
    def _items_for_stage(
        cls,
        ticket,
        stage: str,
        target_statuses: set[str],
    ) -> list[ProductionItem]:
        """Return items from the ticket whose planning has the given stage in one of target_statuses."""
        result = []
        for item in ticket.items.all():
            planning = {
                unicodedata.normalize("NFC", k): v
                for k, v in (item.planning or {}).items()
            }
            if stage not in planning:
                continue

            info = planning[stage] or {}
            status = info.get("status") or ProductionItemStatus.PENDING
            if status in target_statuses:
                result.append(item)

        return result

    @classmethod
    def _line_has_active_sessions(cls, assembly_line: AssemblyLine) -> bool:
        """Return True if any worker has an open session on any item
        belonging to this assembly line's tickets."""
        ticket_ids = list(
            assembly_line.tickets.values_list("pk", flat=True)
        )
        if not ticket_ids:
            return False
        return ProductionItemSession.objects.filter(
            production_item__ticket_id__in=ticket_ids,
            status=ProductionItemSessionStatus.IN_PROGRESS,
            end_time__isnull=True,
        ).exists()

