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
        show_planning_type: bool = False,
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
                show_planning_type=show_planning_type,
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
        start_dt = datetime.datetime.combine(date_from, daily_start)
        end_dt = datetime.datetime.combine(date_to, daily_end)
        if end_dt <= start_dt:
            if date_from == date_to:
                raise ValidationError(_("Daily end time must be after daily start time."))
            raise ValidationError(_("End date/time must be after start date/time."))
        if not days:
            raise ValidationError(_("The selected date range contains no working days."))

    MAX_REPEAT_OCCURRENCES = 100

    # Apple-calendar-style fixed frequencies, in days (None = calendar stepping)
    _REPEAT_DAY_STEPS = {"daily": 1, "weekly": 7, "biweekly": 14}

    @staticmethod
    def _add_months(base: date, months: int) -> date:
        """base shifted by N months, clamped to the last day of short months."""
        import calendar as _calendar

        year = base.year + (base.month - 1 + months) // 12
        month = (base.month - 1 + months) % 12 + 1
        last_day = _calendar.monthrange(year, month)[1]
        return date(year, month, min(base.day, last_day))

    @classmethod
    def _expand_repeat_windows(
        cls,
        date_from: date,
        date_to: date,
        repeat: str,
        repeat_until: "date | None",
        exclude_weekends: bool,
        repeat_every_days: int | None = None,
    ) -> list[tuple[date, date]]:
        """
        Expand a base date window into repeated same-day windows, Apple-calendar
        style: never / daily / weekly / biweekly / monthly / yearly / custom,
        up to and including repeat_until. Custom uses repeat_every_days as the
        day interval. Monthly and yearly occurrences falling on a shorter month
        are clamped to its last day. Weekend occurrences are silently skipped
        when exclude_weekends is on.
        """
        if not repeat or repeat == "never":
            return [(date_from, date_to)]
        if repeat not in ("daily", "weekly", "biweekly", "monthly", "yearly", "custom"):
            raise ValidationError(_("Unknown repeat frequency."))
        if date_from != date_to:
            raise ValidationError(_("Repetition is only available for same-day plannings."))
        if not repeat_until:
            raise ValidationError(_("Choose an end date for the repetition."))
        if repeat_until < date_from:
            raise ValidationError(_("The repeat-until date must be on or after the start date."))
        if repeat == "custom":
            if not repeat_every_days or repeat_every_days < 1:
                raise ValidationError(_("Enter how many days between repetitions."))
            if repeat_every_days > 365:
                raise ValidationError(_("Custom interval must be 365 days or fewer."))

        day_step = cls._REPEAT_DAY_STEPS.get(repeat)
        if repeat == "custom":
            day_step = repeat_every_days
        windows: list[tuple[date, date]] = []
        occurrence_index = 0
        while True:
            if occurrence_index > cls.MAX_REPEAT_OCCURRENCES:
                raise ValidationError(
                    _("Too many repetitions (over %(max)s). Bring the end date closer.")
                    % {"max": cls.MAX_REPEAT_OCCURRENCES}
                )
            if day_step is not None:
                current = date_from + timedelta(days=day_step * occurrence_index)
            elif repeat == "monthly":
                current = cls._add_months(date_from, occurrence_index)
            else:  # yearly
                current = cls._add_months(date_from, 12 * occurrence_index)
            if current > repeat_until:
                break
            if not exclude_weekends or current.weekday() < 5:
                windows.append((current, current))
            occurrence_index += 1

        if not windows:
            raise ValidationError(
                _("The repetition produces no plannings: every occurrence falls on a weekend.")
            )
        return windows

    @staticmethod
    def _shift_following_plannings(new_plannings: list["ResourcePlanning"]) -> None:
        """
        For each not-yet-saved planning, push the same resource's colliding
        plannings of the same local day forward by its duration — domino style:
        a plan is only shifted if it overlaps the new planning or an
        already-shifted plan; the chain stops at the first gap that absorbs the
        shift. Raises ValidationError if any pushed planning would end up on a
        different calendar day.
        """
        from management.models import ResourcePlanning, ResourcePlanningStatus

        for p in new_plannings:
            duration = p.end - p.start
            day = timezone.localtime(p.start).date()
            qs = ResourcePlanning.objects.filter(
                resource_content_type=p.resource_content_type,
                resource_object_id=p.resource_object_id,
                start__gte=p.start,
            ).exclude(status=ResourcePlanningStatus.CANCELLED).order_by("start")

            occupied_end = p.end
            for existing in qs:
                if timezone.localtime(existing.start).date() != day:
                    continue
                if existing.start >= occupied_end:
                    break  # fits after the chain — later plans fit too
                new_start = existing.start + duration
                new_end = existing.end + duration
                if (
                    timezone.localtime(new_start).date() != timezone.localtime(existing.start).date()
                    or timezone.localtime(new_end).date() != timezone.localtime(existing.end).date()
                ):
                    raise ValidationError(
                        _(
                            "Cannot insert this planning: %(planning)s would be pushed "
                            "from %(old_end)s to %(new_end)s, crossing into the next day. "
                            "Nothing was saved."
                        )
                        % {
                            "planning": str(existing),
                            "old_end": timezone.localtime(existing.end).strftime("%H:%M"),
                            "new_end": timezone.localtime(new_end).strftime("%d/%m %H:%M"),
                        }
                    )
                existing.start = new_start
                existing.end = new_end
                existing.save(update_fields=["start", "end"])
                occupied_end = new_end

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
        show_planning_type: bool = False,
        shift_following: bool = False,
        repeat: str = "never",
        repeat_until: "date | None" = None,
        repeat_every_days: int | None = None,
    ) -> list["ResourcePlanning"]:
        from management.models import ResourcePlanning, ResourcePlanningGroup

        windows = cls._expand_repeat_windows(
            date_from, date_to, repeat, repeat_until, exclude_weekends, repeat_every_days,
        )
        repeating = len(windows) > 1
        if repeating and shift_following:
            raise ValidationError(_("Repetition cannot be combined with insert mode."))

        first_from, first_to = windows[0]
        days = cls.iter_working_days(first_from, first_to, exclude_weekends)
        cls._validate_bulk_inputs(resources, first_from, first_to, daily_start, daily_end, days)

        with transaction.atomic():
            # Repetition series are always grouped so they can be managed together
            group = (
                ResourcePlanningGroup.objects.create(company=company, is_repetition=repeating)
                if (is_group or repeating) else None
            )
            plannings = []
            for window_from, window_to in windows:
                plannings += cls._build_plannings(
                    company=company, project_file=project_file, planning_type=planning_type,
                    status=status, notes=notes, resources=resources,
                    date_from=window_from, date_to=window_to,
                    daily_start=daily_start, daily_end=daily_end,
                    exclude_weekends=exclude_weekends, group=group,
                    show_planning_type=show_planning_type,
                )
            if shift_following:
                cls._shift_following_plannings(plannings)
            if repeating:
                cls._validate_no_internal_overlaps(plannings)
            cls._validate_no_overlaps(plannings)
            ResourcePlanning.objects.bulk_create(plannings)

        return plannings

    @staticmethod
    def _validate_no_internal_overlaps(plannings: list["ResourcePlanning"]) -> None:
        """Reject overlapping windows among the not-yet-saved plannings themselves."""
        by_resource: dict[tuple[int, int], list] = {}
        for p in plannings:
            key = (p.resource_content_type_id, p.resource_object_id)
            by_resource.setdefault(key, []).append(p)
        for items in by_resource.values():
            items.sort(key=lambda p: p.start)
            for prev, nxt in zip(items, items[1:]):
                if nxt.start < prev.end:
                    raise ValidationError(
                        _("Two plan lines overlap for resource %(resource)s (%(start)s–%(end)s).")
                        % {
                            "resource": f"{prev.resource_content_type} #{prev.resource_object_id}",
                            "start": nxt.start.strftime("%d/%m %H:%M"),
                            "end": prev.end.strftime("%d/%m %H:%M"),
                        }
                    )

    @classmethod
    def create_multi_plannings(
        cls,
        *,
        company,
        status: str,
        resources: list[tuple[ContentType, int]],
        lines: list[dict],
    ) -> list["ResourcePlanning"]:
        """
        Create several independent (ungrouped) plannings sharing company, status
        and resources. Each line dict provides its own project_file, planning_type,
        date_from, date_to, daily_start, daily_end, exclude_weekends,
        show_planning_type, notes and optional repeat fields (repeat,
        repeat_until, repeat_every_days for custom). A repeating line's
        occurrences are created as a repetition-series group so they can be
        managed together.
        """
        from management.models import ResourcePlanning, ResourcePlanningGroup

        if not lines:
            raise ValidationError(_("Add at least one plan line."))

        with transaction.atomic():
            plannings: list[ResourcePlanning] = []
            for line in lines:
                exclude_weekends = line.get("exclude_weekends", True)
                windows = cls._expand_repeat_windows(
                    line["date_from"], line["date_to"],
                    line.get("repeat", "never"),
                    line.get("repeat_until"),
                    exclude_weekends,
                    line.get("repeat_every_days"),
                )
                first_from, first_to = windows[0]
                days = cls.iter_working_days(first_from, first_to, exclude_weekends)
                cls._validate_bulk_inputs(
                    resources, first_from, first_to,
                    line["daily_start"], line["daily_end"], days,
                )
                line_group = (
                    ResourcePlanningGroup.objects.create(company=company, is_repetition=True)
                    if len(windows) > 1 else None
                )
                for window_from, window_to in windows:
                    plannings += cls._build_plannings(
                        company=company, project_file=line["project_file"],
                        planning_type=line["planning_type"],
                        status=status, notes=line.get("notes", ""), resources=resources,
                        date_from=window_from, date_to=window_to,
                        daily_start=line["daily_start"], daily_end=line["daily_end"],
                        exclude_weekends=exclude_weekends, group=line_group,
                        show_planning_type=line.get("show_planning_type", False),
                    )

            cls._validate_no_internal_overlaps(plannings)
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
        show_planning_type: bool = False,
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
                show_planning_type=show_planning_type,
            )
            cls._validate_no_overlaps(plannings, exclude_pks=old_pks)
            group.plannings.all().delete()
            ResourcePlanning.objects.bulk_create(plannings)

        return plannings

    @classmethod
    def update_series(
        cls,
        group_pk: int,
        *,
        project_file,
        planning_type,
        status: str,
        notes: str = "",
        show_planning_type: bool = False,
        daily_start: datetime.time,
        daily_end: datetime.time,
    ) -> list["ResourcePlanning"]:
        """
        Apply changes to every occurrence of a repetition series without
        touching their dates: metadata is replaced, and the daily hours are
        re-applied on each occurrence's own day.
        """
        from management.models import ResourcePlanning, ResourcePlanningGroup

        if daily_end <= daily_start:
            raise ValidationError(_("Daily end time must be after daily start time."))

        with transaction.atomic():
            group = ResourcePlanningGroup.objects.get(pk=group_pk)
            plannings = list(group.plannings.all())
            member_pks = [p.pk for p in plannings]
            for p in plannings:
                p.project_file = project_file
                p.planning_type = planning_type
                p.status = status
                p.notes = notes
                p.show_planning_type = show_planning_type
                p.start = timezone.make_aware(datetime.datetime.combine(
                    timezone.localtime(p.start).date(), daily_start
                ))
                p.end = timezone.make_aware(datetime.datetime.combine(
                    timezone.localtime(p.end).date(), daily_end
                ))
            cls._validate_no_overlaps(plannings, exclude_pks=member_pks)
            for p in plannings:
                p.save()

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
    def delete_group(group_pk: int) -> None:
        """Delete every planning in the group and the group itself."""
        from management.models import ResourcePlanningGroup

        with transaction.atomic():
            group = ResourcePlanningGroup.objects.get(pk=group_pk)
            group.plannings.all().delete()
            group.delete()

    @staticmethod
    def detach_planning_from_group(planning: "ResourcePlanning") -> None:
        ResourcePlanningService._detach_planning_from_group(planning)

    @staticmethod
    def _detach_planning_from_group(planning: "ResourcePlanning") -> None:
        """
        Remove a single planning from its group without affecting other members.

        If the group is left with one or zero plannings, dissolve it entirely.
        """
        from management.models import ResourcePlanning, ResourcePlanningGroup

        group_pk = planning.group_id
        if not group_pk:
            return

        planning.group = None
        planning.save(update_fields=["group"])

        if not ResourcePlanning.objects.filter(group_id=group_pk).exists():
            ResourcePlanningGroup.objects.filter(pk=group_pk).delete()

    @staticmethod
    def delete_day_from_planning(planning_pk: int, day_date: date) -> None:
        """
        Remove a single calendar day from a planning's date range.

        If the planning belongs to a group, it is detached first so only this
        resource is modified; the rest of the group stays synchronized.

        - Single-day planning → deleted entirely.
        - First day  → start moves to the next day.
        - Last day   → end moves to the previous day.
        - Middle day → split into two plannings with a gap on that day.
        """
        from management.models import ResourcePlanning

        planning = ResourcePlanning.objects.get(pk=planning_pk)
        ResourcePlanningService._detach_planning_from_group(planning)

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
                    group=None,
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
create_multi_plannings   = ResourcePlanningService.create_multi_plannings
update_group             = ResourcePlanningService.update_group
update_series            = ResourcePlanningService.update_series
update_planning          = ResourcePlanningService.update_planning
dissolve_group           = ResourcePlanningService.dissolve_group
delete_group             = ResourcePlanningService.delete_group
detach_planning_from_group = ResourcePlanningService.detach_planning_from_group
get_group_plannings      = ResourcePlanningService.get_group_plannings
delete_day_from_planning = ResourcePlanningService.delete_day_from_planning
