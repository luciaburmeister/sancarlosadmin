from __future__ import annotations

from datetime import date, timedelta
from typing import TYPE_CHECKING

from django.utils import timezone

if TYPE_CHECKING:
    from django.contrib.auth.models import AbstractBaseUser
    from management.models import ResourcePlanning, ResourcePlanningBacklogItem


class ResourcePlanningBacklogService:

    @staticmethod
    def suggest_date_to(date_from: date, estimated_days: int, exclude_weekends: bool) -> date:
        """Return the date that covers `estimated_days` working days from `date_from`."""
        remaining = max(estimated_days, 1)
        current = date_from
        while True:
            if not exclude_weekends or current.weekday() < 5:
                remaining -= 1
                if remaining == 0:
                    return current
            current += timedelta(days=1)

    @staticmethod
    def mark_scheduled(item: ResourcePlanningBacklogItem, plannings: list[ResourcePlanning]) -> None:
        """Record a scheduling. Reusable items stay pending (scheduled_at /
        scheduled_group then track the last use); one-shot items are closed."""
        from management.models import ResourcePlanningBacklogStatus

        if not item.is_reusable:
            item.status = ResourcePlanningBacklogStatus.SCHEDULED
        item.scheduled_group = plannings[0].group if plannings and plannings[0].group_id else None
        item.scheduled_at = timezone.now()
        item.save(update_fields=["status", "scheduled_group", "scheduled_at"])

    @staticmethod
    def create_from_planning(
        planning: ResourcePlanning, created_by: AbstractBaseUser | None = None
    ) -> ResourcePlanningBacklogItem:
        """Copy an existing planning into the backlog as a reusable template."""
        from management.models import ResourcePlanningBacklogItem
        from management.services.resource_planning import iter_working_days

        local_start = timezone.localtime(planning.start)
        local_end = timezone.localtime(planning.end)
        estimated_days = len(
            iter_working_days(local_start.date(), local_end.date(), planning.exclude_weekends)
        ) or 1
        return ResourcePlanningBacklogItem.objects.create(
            company=planning.company,
            project_file=planning.project_file,
            planning_type=planning.planning_type,
            notes=planning.notes,
            show_planning_type=planning.show_planning_type,
            estimated_days=estimated_days,
            daily_start=local_start.time(),
            daily_end=local_end.time(),
            exclude_weekends=planning.exclude_weekends,
            is_reusable=True,
            created_by=created_by,
        )

    @staticmethod
    def reopen(item: ResourcePlanningBacklogItem) -> None:
        from management.models import ResourcePlanningBacklogStatus

        item.status = ResourcePlanningBacklogStatus.PENDING
        item.scheduled_group = None
        item.scheduled_at = None
        item.save(update_fields=["status", "scheduled_group", "scheduled_at"])


suggest_date_to = ResourcePlanningBacklogService.suggest_date_to
mark_scheduled = ResourcePlanningBacklogService.mark_scheduled
reopen_backlog_item = ResourcePlanningBacklogService.reopen
create_backlog_item_from_planning = ResourcePlanningBacklogService.create_from_planning
