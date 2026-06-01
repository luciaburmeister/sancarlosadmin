import unicodedata
import logging

from django.core.exceptions import ValidationError
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from production.models import (
    AssemblyLine,
    AssemblyLineStatus,
    ProductionItem,
    ProductionItemStatus,
    ProductionTicketStatus,
)
from production.services.stages_to_sessions import StagesToSessionsService
from company.models import Employee

logger = logging.getLogger(__name__)


class AssemblyLineService:
    """Orchestrates bulk start / pause / finish operations for an AssemblyLine.

    Stages are processed **sequentially**: only the current (first incomplete)
    stage can be started. Once all items are done for that stage, the next
    stage becomes the current one.
    """

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @classmethod
    def get_current_stage(cls, assembly_line: AssemblyLine) -> str | None:
        """Return the first stage that is not fully done across all tickets.

        Returns None when every stage is complete.
        """
        stages = cls._normalize_stages(assembly_line)
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
    def start_line(cls, assembly_line: AssemblyLine, employee: Employee) -> dict:
        """Start the current stage for ALL pending items across ALL tickets.

        Returns a summary dict: {started: int, skipped: int, stage: str, errors: [str]}
        """
        cls._assert_can_transition(
            assembly_line,
            allowed={AssemblyLineStatus.READY, AssemblyLineStatus.PAUSED, AssemblyLineStatus.DRAFT},
        )

        current_stage = cls.get_current_stage(assembly_line)
        if current_stage is None:
            return {"started": 0, "skipped": 0, "stage": None, "errors": []}

        started = 0
        skipped = 0
        errors: list[str] = []

        for ticket in cls._active_tickets(assembly_line):
            items = cls._items_for_stage(ticket, current_stage, target_statuses={
                ProductionItemStatus.PENDING,
                ProductionItemStatus.IN_PROGRESS,
            })
            if not items:
                skipped += 1
                continue

            try:
                count = StagesToSessionsService.start_activity(
                    stage_name=current_stage, items=items, employee=employee,
                    skip_worker_check=True,
                )
                started += count
            except ValidationError as exc:
                errors.append(f"Ticket #{ticket.pk} [{current_stage}]: {exc.message}")
            except Exception as exc:
                logger.exception("start_line error on ticket %s stage %s", ticket.pk, current_stage)
                errors.append(f"Ticket #{ticket.pk} [{current_stage}]: {exc}")

        now = timezone.now()
        AssemblyLine.objects.filter(pk=assembly_line.pk).update(
            status=AssemblyLineStatus.IN_PROGRESS,
            started_by=employee,
            started_at=assembly_line.started_at or now,
        )
        assembly_line.status = AssemblyLineStatus.IN_PROGRESS
        assembly_line.started_by = employee
        assembly_line.started_at = assembly_line.started_at or now

        return {"started": started, "skipped": skipped, "stage": current_stage, "errors": errors}

    @classmethod
    def pause_line(cls, assembly_line: AssemblyLine, employee: Employee) -> dict:
        """Pause the current stage for all active items across all tickets.

        Returns a summary dict: {paused: int, skipped: int, stage: str, errors: [str]}
        """
        cls._assert_can_transition(
            assembly_line,
            allowed={AssemblyLineStatus.IN_PROGRESS},
        )

        current_stage = cls.get_current_stage(assembly_line)
        if current_stage is None:
            return {"paused": 0, "skipped": 0, "stage": None, "errors": []}

        paused = 0
        skipped = 0
        errors: list[str] = []

        for ticket in cls._active_tickets(assembly_line):
            items = cls._items_for_stage(ticket, current_stage, target_statuses={
                ProductionItemStatus.IN_PROGRESS,
            })
            if not items:
                skipped += 1
                continue

            try:
                count = StagesToSessionsService.leave_activity(
                    stage_name=current_stage, items=items, employee=employee,
                )
                paused += count
            except ValidationError as exc:
                errors.append(f"Ticket #{ticket.pk} [{current_stage}]: {exc.message}")
            except Exception as exc:
                logger.exception("pause_line error on ticket %s stage %s", ticket.pk, current_stage)
                errors.append(f"Ticket #{ticket.pk} [{current_stage}]: {exc}")

        AssemblyLine.objects.filter(pk=assembly_line.pk).update(
            status=AssemblyLineStatus.PAUSED,
        )
        assembly_line.status = AssemblyLineStatus.PAUSED

        return {"paused": paused, "skipped": skipped, "stage": current_stage, "errors": errors}

    @classmethod
    def finish_line(cls, assembly_line: AssemblyLine, employee: Employee) -> dict:
        """Finish the current stage for ALL in-progress items across ALL tickets.

        If all stages are now complete, marks the line as DONE.

        Returns a summary dict: {finished: int, skipped: int, stage: str, errors: [str]}
        """
        cls._assert_can_transition(
            assembly_line,
            allowed={AssemblyLineStatus.IN_PROGRESS},
        )

        current_stage = cls.get_current_stage(assembly_line)
        if current_stage is None:
            return {"finished": 0, "skipped": 0, "stage": None, "errors": []}

        finished = 0
        skipped = 0
        errors: list[str] = []

        for ticket in cls._active_tickets(assembly_line):
            items = cls._items_for_stage(ticket, current_stage, target_statuses={
                ProductionItemStatus.IN_PROGRESS,
            })
            if not items:
                skipped += 1
                continue

            try:
                count = StagesToSessionsService.finish_activity(
                    stage_name=current_stage, items=items, employee=employee,
                )
                finished += count
            except ValidationError as exc:
                errors.append(f"Ticket #{ticket.pk} [{current_stage}]: {exc.message}")
            except Exception as exc:
                logger.exception("finish_line error on ticket %s stage %s", ticket.pk, current_stage)
                errors.append(f"Ticket #{ticket.pk} [{current_stage}]: {exc}")

        # Check if ALL stages are now done
        assembly_line.refresh_from_db()
        next_stage = cls.get_current_stage(assembly_line)

        if next_stage is None:
            now = timezone.now()
            AssemblyLine.objects.filter(pk=assembly_line.pk).update(
                status=AssemblyLineStatus.DONE,
                finished_at=now,
            )
            assembly_line.status = AssemblyLineStatus.DONE
            assembly_line.finished_at = now
        else:
            # Move back to READY so the user can start the next stage
            AssemblyLine.objects.filter(pk=assembly_line.pk).update(
                status=AssemblyLineStatus.READY,
            )
            assembly_line.status = AssemblyLineStatus.READY

        return {"finished": finished, "skipped": skipped, "stage": current_stage, "errors": errors}

    @classmethod
    def get_line_progress(cls, assembly_line: AssemblyLine) -> dict:
        """Return aggregated progress for the assembly line with per-stage detail.

        Returns: {
            total_tickets, total_items, pending, in_progress, done,
            stages: [{name, pending, in_progress, done, is_current}],
            current_stage: str | None,
            tickets: [{id, name, ...}]
        }
        """
        stages = cls._normalize_stages(assembly_line)
        current_stage = cls.get_current_stage(assembly_line)

        tickets_data = []
        total_items = 0
        total_pending = 0
        total_in_progress = 0
        total_done = 0

        # Per-stage aggregates
        stage_aggregates = {s: {"pending": 0, "in_progress": 0, "done": 0} for s in stages}

        for ticket in assembly_line.tickets.select_related("company").prefetch_related(
            "items__component"
        ):
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
