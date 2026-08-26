"""Skip or bulk-abandon a driver's cuba/porte plannings.

Marks the planning `not_completed` with a computed `abandoned_stage`
(AssetPlanning.compute_abandoned_stage), plus who/why/when, so dispatch
can see and reschedule it from the asset schedule view.

A cuba left picked up is never blocked — the driver resolves it via
pending dropoff on the next-task screen. Porte still blocks when that
same planning is in transit: a load left on the truck has no equivalent
self-resolve path.
"""
from __future__ import annotations

from django.db import transaction
from django.utils import timezone

from assets.models import (
    AbandonedStageChoices,
    AssetPlanning,
    PlanningStatus,
    PlanningType,
    DRIVER_TASK_PLANNING_TYPES,
)

BLOCKED_ABANDON_STAGES = {
    AbandonedStageChoices.PICKED_UP_PENDING_DELIVERY,
}


class PlanningAbandonBlockedError(Exception):
    """Raised when the driver has a picked-up-but-not-dropped-off porte."""

    def __init__(self, planning: AssetPlanning):
        self.planning = planning
        super().__init__(f"Planning {planning.id} is still carrying an asset")


class PlanningAbandoner:
    def abandon(self, employee, planning: AssetPlanning, reason: str) -> AssetPlanning:
        with transaction.atomic():
            planning = AssetPlanning.objects.select_for_update().get(pk=planning.pk)
            stage = planning.compute_abandoned_stage()
            if (
                planning.planning_type == PlanningType.PORTE
                and stage in BLOCKED_ABANDON_STAGES
            ):
                raise PlanningAbandonBlockedError(planning)
            self._mark_abandoned(planning, employee, reason, stage, timezone.now())
        return planning

    def abandon_remaining(self, employee, reason: str = "") -> list[AssetPlanning]:
        today = timezone.now().date()
        with transaction.atomic():
            qs = AssetPlanning.objects.select_for_update().filter(
                assigned_to=employee,
                status__in=[PlanningStatus.PENDING, PlanningStatus.IN_TRANSIT],
                planning_type__in=DRIVER_TASK_PLANNING_TYPES,
                scheduled_date=today,
            )
            plannings = list(qs)
            stages = {planning.id: planning.compute_abandoned_stage() for planning in plannings}
            blocked = next(
                (
                    p for p in plannings
                    if p.planning_type == PlanningType.PORTE and stages[p.id] in BLOCKED_ABANDON_STAGES
                ),
                None,
            )
            if blocked is not None:
                raise PlanningAbandonBlockedError(blocked)
            abandoned = []
            now = timezone.now()
            for planning in plannings:
                self._mark_abandoned(planning, employee, reason, stages[planning.id], now)
                abandoned.append(planning)
        return abandoned

    @staticmethod
    def _mark_abandoned(planning, employee, reason, stage, when):
        planning.abandoned_stage = stage
        planning.status = PlanningStatus.NOT_COMPLETED
        planning.abandoned_at = when
        planning.abandoned_reason = reason
        planning.abandoned_by = employee
        planning.save(update_fields=[
            "status", "abandoned_stage", "abandoned_at", "abandoned_reason", "abandoned_by",
        ])
