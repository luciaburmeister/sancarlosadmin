"""
"No puedo hacer más" — bulk-abandon a driver's remaining cuba/porte plannings for today.

Sets each still-open planning to `not_completed` with a computed
`abandoned_stage` (AssetPlanning.compute_abandoned_stage) so dispatch can see
and reschedule it from the asset schedule view.
"""
from __future__ import annotations

from django.db import transaction
from django.utils import timezone

from assets.models import AssetPlanning, PlanningStatus, PlanningType


class PlanningAbandoner:
    def abandon_remaining(self, employee) -> list[AssetPlanning]:
        today = timezone.now().date()
        abandoned = []
        with transaction.atomic():
            qs = AssetPlanning.objects.select_for_update().filter(
                assigned_to=employee, status__in=[PlanningStatus.PENDING, PlanningStatus.IN_TRANSIT],
                planning_type__in=[PlanningType.CUBA, PlanningType.PORTE], scheduled_date=today,
            )
            for planning in qs:
                planning.abandoned_stage = planning.compute_abandoned_stage()
                planning.status = PlanningStatus.NOT_COMPLETED
                planning.abandoned_at = timezone.now()
                planning.save(update_fields=["status", "abandoned_stage", "abandoned_at"])
                abandoned.append(planning)
        return abandoned
