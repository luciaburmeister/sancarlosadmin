from __future__ import annotations

import datetime
from datetime import date, timedelta
from typing import TYPE_CHECKING

from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

if TYPE_CHECKING:
    from django.db.models import QuerySet
    from management.models import ResourcePlanning, ResourcePlanningGroup


class ResourcePlanningService:

    @staticmethod
    def iter_working_days(date_from: date, date_to: date, exclude_weekends: bool) -> list[date]:
        """Return all dates in [date_from, date_to], optionally skipping Sat/Sun."""
        days, current = [], date_from
        while current <= date_to:
            if not exclude_weekends or current.weekday() < 5:
                days.append(current)
            current += timedelta(days=1)
        return days

    @staticmethod
    def _build_plannings(
        *,
        company,
        project_file,
        planning_type,
        status: str,
        notes: str,
        resources: list[tuple[ContentType, int]],
        date_from: date,
        date_to: date,
        daily_start: datetime.time,
        daily_end: datetime.time,
        exclude_weekends: bool,
        group: "ResourcePlanningGroup | None",
    ) -> list["ResourcePlanning"]:
        from management.models import ResourcePlanning

        start_dt = timezone.make_aware(datetime.datetime.combine(date_from, daily_start))
        end_dt = timezone.make_aware(datetime.datetime.combine(date_to, daily_end))
        return [
            ResourcePlanning(
                company=company,
                project_file=project_file,
                planning_type=planning_type,
                status=status,
                notes=notes,
                exclude_weekends=exclude_weekends,
                group=group,
                resource_content_type=ct,
                resource_object_id=obj_id,
                start=start_dt,
                end=end_dt,
            )
            for ct, obj_id in resources
        ]

    @staticmethod
    def _validate_no_overlaps(
        plannings: list["ResourcePlanning"],
        exclude_pks: list[int] | None = None,
    ) -> None:
        from management.models import ResourcePlanning, ResourcePlanningStatus

        for p in plannings:
            qs = ResourcePlanning.objects.filter(
                resource_content_type=p.resource_content_type,
                resource_object_id=p.resource_object_id,
                start__lt=p.end,
                end__gt=p.start,
            ).exclude(status=ResourcePlanningStatus.CANCELLED)
            if exclude_pks:
                qs = qs.exclude(pk__in=exclude_pks)
            if qs.exists():
                raise ValidationError(
                    _("Resource %(resource)s already has an active planning overlapping %(start)s–%(end)s.")
                    % {
                        "resource": f"{p.resource_content_type} #{p.resource_object_id}",
                        "start": p.start.strftime("%d/%m %H:%M"),
                        "end": p.end.strftime("%H:%M"),
                    }
                )

    @staticmethod
    def _validate_bulk_inputs(
        resources: list,
        date_from: date,
        date_to: date,
        daily_start: datetime.time,
        daily_end: datetime.time,
        days: list[date],
    ) -> None:
        if not resources:
            raise ValidationError(_("At least one resource is required."))
        if date_to < date_from:
            raise ValidationError(_("End date must be on or after start date."))
        if daily_end <= daily_start:
            raise ValidationError(_("Daily end time must be after daily start time."))
        if not days:
            raise ValidationError(_("The selected date range contains no working days."))

    @classmethod
    def create_plannings(
        cls,
        *,
        company,
        project_file,
        planning_type,
        status: str,
        notes: str = "",
        resources: list[tuple[ContentType, int]],
        date_from: date,
        date_to: date,
        daily_start: datetime.time,
        daily_end: datetime.time,
        exclude_weekends: bool = True,
        is_group: bool = False,
    ) -> list["ResourcePlanning"]:
        from management.models import ResourcePlanning, ResourcePlanningGroup

        days = cls.iter_working_days(date_from, date_to, exclude_weekends)
        cls._validate_bulk_inputs(resources, date_from, date_to, daily_start, daily_end, days)

        with transaction.atomic():
            group = ResourcePlanningGroup.objects.create(company=company) if is_group else None
            plannings = cls._build_plannings(
                company=company, project_file=project_file, planning_type=planning_type,
                status=status, notes=notes, resources=resources,
                date_from=date_from, date_to=date_to,
                daily_start=daily_start, daily_end=daily_end,
                exclude_weekends=exclude_weekends, group=group,
            )
            cls._validate_no_overlaps(plannings)
            ResourcePlanning.objects.bulk_create(plannings)

        return plannings

    @classmethod
    def update_group(
        cls,
        group_pk: int,
        *,
        company,
        project_file,
        planning_type,
        status: str,
        notes: str = "",
        resources: list[tuple[ContentType, int]],
        date_from: date,
        date_to: date,
        daily_start: datetime.time,
        daily_end: datetime.time,
        exclude_weekends: bool = True,
    ) -> list["ResourcePlanning"]:
        from management.models import ResourcePlanning, ResourcePlanningGroup

        days = cls.iter_working_days(date_from, date_to, exclude_weekends)
        cls._validate_bulk_inputs(resources, date_from, date_to, daily_start, daily_end, days)

        with transaction.atomic():
            group = ResourcePlanningGroup.objects.get(pk=group_pk)
            old_pks = list(group.plannings.values_list("pk", flat=True))
            plannings = cls._build_plannings(
                company=company, project_file=project_file, planning_type=planning_type,
                status=status, notes=notes, resources=resources,
                date_from=date_from, date_to=date_to,
                daily_start=daily_start, daily_end=daily_end,
                exclude_weekends=exclude_weekends, group=group,
            )
            cls._validate_no_overlaps(plannings, exclude_pks=old_pks)
            group.plannings.all().delete()
            ResourcePlanning.objects.bulk_create(plannings)

        return plannings

    @staticmethod
    def update_planning(planning_pk: int, **fields) -> "ResourcePlanning":
        from management.models import ResourcePlanning

        with transaction.atomic():
            planning = ResourcePlanning.objects.get(pk=planning_pk)
            for field, value in fields.items():
                setattr(planning, field, value)
            planning.full_clean()
            planning.save()

        return planning

    @staticmethod
    def dissolve_group(group_pk: int) -> None:
        from management.models import ResourcePlanning, ResourcePlanningGroup

        with transaction.atomic():
            ResourcePlanning.objects.filter(group_id=group_pk).update(group=None)
            ResourcePlanningGroup.objects.filter(pk=group_pk).delete()

    @staticmethod
    def get_group_plannings(group_pk: int) -> "QuerySet":
        from management.models import ResourcePlanning

        return ResourcePlanning.objects.filter(group_id=group_pk).order_by("start")

    @staticmethod
    def delete_day_from_planning(planning_pk: int, day_date: date) -> None:
        """
        Remove a single calendar day from a planning's date range.

        - Single-day planning → deleted entirely.
        - First day  → start moves to the next day.
        - Last day   → end moves to the previous day.
        - Middle day → split into two plannings with a gap on that day.
        """
        from management.models import ResourcePlanning

        planning = ResourcePlanning.objects.get(pk=planning_pk)
        local_start = timezone.localtime(planning.start)
        local_end   = timezone.localtime(planning.end)

        start_date       = local_start.date()
        end_date         = local_end.date()
        daily_start_time = local_start.time()
        daily_end_time   = local_end.time()

        with transaction.atomic():
            if start_date == end_date:
                planning.delete()

            elif day_date == start_date:
                planning.start = timezone.make_aware(
                    datetime.datetime.combine(start_date + timedelta(days=1), daily_start_time)
                )
                planning.save(update_fields=["start"])

            elif day_date == end_date:
                planning.end = timezone.make_aware(
                    datetime.datetime.combine(end_date - timedelta(days=1), daily_end_time)
                )
                planning.save(update_fields=["end"])

            else:
                original_end = planning.end
                planning.end = timezone.make_aware(
                    datetime.datetime.combine(day_date - timedelta(days=1), daily_end_time)
                )
                planning.save(update_fields=["end"])

                ResourcePlanning.objects.create(
                    company=planning.company,
                    project_file=planning.project_file,
                    planning_type=planning.planning_type,
                    status=planning.status,
                    notes=planning.notes,
                    exclude_weekends=planning.exclude_weekends,
                    group=planning.group,
                    resource_content_type=planning.resource_content_type,
                    resource_object_id=planning.resource_object_id,
                    start=timezone.make_aware(
                        datetime.datetime.combine(day_date + timedelta(days=1), daily_start_time)
                    ),
                    end=original_end,
                )


# ── Module-level aliases (backward compatibility) ─────────────────────────────

iter_working_days        = ResourcePlanningService.iter_working_days
create_plannings         = ResourcePlanningService.create_plannings
update_group             = ResourcePlanningService.update_group
update_planning          = ResourcePlanningService.update_planning
dissolve_group           = ResourcePlanningService.dissolve_group
get_group_plannings      = ResourcePlanningService.get_group_plannings
delete_day_from_planning = ResourcePlanningService.delete_day_from_planning
