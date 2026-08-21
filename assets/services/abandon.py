"""
"No puedo hacer más" — bulk-abandon a driver's remaining cuba/porte plannings for today.

Sets each still-open planning to `not_completed` with a computed
`abandoned_stage` (AssetPlanning.compute_abandoned_stage), plus who/why/when
(abandoned_by/abandoned_reason/abandoned_at), so dispatch can see and
reschedule it from the asset schedule view.

A cuba left picked up is never blocked here — ending the day mid-cuba is
normal and the driver can't force resolving it on demand. Instead
PlanningNextTaskView (api/fleet/views/attendance.py) blocks them from
starting a different task until they close it out, via Asset.pending_pickup_by.
Porte still blocks: a load left in transit has no equivalent self-resolve path.
"""
from __future__ import annotations

from django.db import transaction
from django.utils import timezone

from assets.models import AbandonedStageChoices, AssetPlanning, PlanningStatus, PlanningType, DRIVER_TASK_PLANNING_TYPES

BLOCKED_ABANDON_STAGES = {
    AbandonedStageChoices.PICKED_UP_PENDING_DELIVERY,
}


class PlanningAbandonBlockedError(Exception):
    """Raised when the driver has a picked-up-but-not-dropped-off cuba/porte."""

    def __init__(self, planning: AssetPlanning):
        self.planning = planning
        super().__init__(f"Planning {planning.id} is still carrying an asset")


class PlanningAbandoner:
    def abandon_remaining(self, employee, reason: str = "") -> list[AssetPlanning]:
        today = timezone.now().date()
        with transaction.atomic():
            qs = AssetPlanning.objects.select_for_update().filter(
                assigned_to=employee, status__in=[PlanningStatus.PENDING, PlanningStatus.IN_TRANSIT],
                planning_type__in=DRIVER_TASK_PLANNING_TYPES, scheduled_date=today,
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
                planning.abandoned_stage = stages[planning.id]
                planning.status = PlanningStatus.NOT_COMPLETED
                planning.abandoned_at = now
                planning.abandoned_reason = reason
                planning.abandoned_by = employee
                planning.save(update_fields=[
                    "status", "abandoned_stage", "abandoned_at", "abandoned_reason", "abandoned_by",
                ])
                abandoned.append(planning)
        return abandoned
