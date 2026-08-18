"""
"No puedo hacer más" — bulk-abandon a driver's remaining cuba/porte plannings for today.

Sets each still-open planning to `not_completed` with a computed
`abandoned_stage` (AssetPlanning.compute_abandoned_stage) so dispatch can see
and reschedule it from the asset schedule view.

Blocked if the driver is currently holding a picked-up cuba/porte that hasn't
been dropped off — that must be resolved (delivered, or the real state
confirmed) before the rest of the day's plan can be abandoned. Otherwise the
carried asset would silently vanish from tracking.
"""
from __future__ import annotations

from django.db import transaction
from django.utils import timezone

from assets.models import AbandonedStageChoices, AssetPlanning, PlanningStatus, PlanningType

BLOCKED_ABANDON_STAGES = {
    AbandonedStageChoices.PICKED_UP_PENDING_POUR,
    AbandonedStageChoices.PICKED_UP_PENDING_DELIVERY,
}


class PlanningAbandonBlockedError(Exception):
    """Raised when the driver has a picked-up-but-not-dropped-off cuba/porte."""

    def __init__(self, planning: AssetPlanning):
        self.planning = planning
        super().__init__(f"Planning {planning.id} is still carrying an asset")


class PlanningAbandoner:
    def abandon_remaining(self, employee) -> list[AssetPlanning]:
        today = timezone.now().date()
        with transaction.atomic():
            qs = AssetPlanning.objects.select_for_update().filter(
                assigned_to=employee, status__in=[PlanningStatus.PENDING, PlanningStatus.IN_TRANSIT],
                planning_type__in=[PlanningType.CUBA, PlanningType.PORTE], scheduled_date=today,
            )
            plannings = list(qs)
            stages = {planning.id: planning.compute_abandoned_stage() for planning in plannings}
            blocked = next((p for p in plannings if stages[p.id] in BLOCKED_ABANDON_STAGES), None)
            if blocked is not None:
                raise PlanningAbandonBlockedError(blocked)
            abandoned = []
            for planning in plannings:
                planning.abandoned_stage = stages[planning.id]
                planning.status = PlanningStatus.NOT_COMPLETED
                planning.abandoned_at = timezone.now()
                planning.save(update_fields=["status", "abandoned_stage", "abandoned_at"])
                abandoned.append(planning)
        return abandoned
