from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta

from django.contrib.contenttypes.models import ContentType
from django.db.models import DurationField, ExpressionWrapper, F, Sum
from django.utils import timezone

from company.models import Employee, EmployeeType, Machine, MachineType
from production.models import (
    PlannedProductionSession,
    PlannedProductionSessionSource,
    ProductionResource,
    ProductionTicket,
)


# ---------------------------------------------------------------------------
# Schedule context
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ScheduleContext:
    """Working-hours window derived from company schedule slots."""

    day_start: time
    day_end: time
    max_days_ahead: int = 30

    @classmethod
    def from_company(cls, company) -> ScheduleContext:
        slots = company.schedule_slots.all().order_by("weekday", "start_time")

        if not slots.exists():
            raise ValueError("Company has no schedule slots configured")

        return cls(
            day_start=max(slot.start_time for slot in slots),
            day_end=min(slot.end_time for slot in slots),
        )


# ---------------------------------------------------------------------------
# Resource helpers
# ---------------------------------------------------------------------------

def get_or_create_resource_for_target(target) -> ProductionResource | None:
    """Ensure a ProductionResource wrapper exists for the given Machine/Employee."""
    if not target:
        return None

    ct = ContentType.objects.get_for_model(target)
    resource, _ = ProductionResource.objects.get_or_create(
        content_type=ct,
        object_id=target.pk,
        defaults={"is_active": True},
        company=target.company,
    )
    return resource


def get_resources(resource_type: MachineType | EmployeeType) -> list[ProductionResource]:
    """Return ProductionResource wrappers for every Machine/Employee of the given type."""
    resource_class = Machine if isinstance(resource_type, MachineType) else Employee

    original_resources = list(resource_class.objects.filter(type=resource_type))

    return [
        get_or_create_resource_for_target(res)
        for res in original_resources
    ]


def possible_resources_for_stage(
    resources: list[ProductionResource],
    day,
    duration: timedelta,
) -> set:
    """Return PKs of resources that still have enough daily capacity for *duration*."""
    start_of_day = timezone.make_aware(datetime.combine(day, time.min))
    end_of_day = timezone.make_aware(datetime.combine(day, time.max))

    time_used_by_resource = dict(
        PlannedProductionSession.objects
        .filter(
            primary_resource__in=resources,
            planned_start__lt=end_of_day,
            planned_end__gt=start_of_day,
        )
        .annotate(
            duration=ExpressionWrapper(
                F("planned_end") - F("planned_start"),
                output_field=DurationField(),
            )
        )
        .values("primary_resource")
        .annotate(total=Sum("duration"))
        .values_list("primary_resource", "total")
    )

    possible = set()
    for res in resources:
        used_time = time_used_by_resource.get(res.pk) or timedelta(0)
        machine = Machine.objects.filter(pk=res.object_id).first()
        capacity = timedelta(minutes=getattr(machine, "daily_capacity_minutes", 0) or 0)
        if used_time + duration <= capacity:
            possible.add(res.pk)

    return possible


# ---------------------------------------------------------------------------
# Session scheduler
# ---------------------------------------------------------------------------

class SessionScheduler:
    """
    Creates PlannedProductionSessions for a ProductionTicket.

    Public API:
        schedule_item(production_ticket, stage_resources, schedule_ctx, search_start)
    """

    # -- public entry point -------------------------------------------------

    @classmethod
    def schedule_item(
        cls,
        production_ticket: ProductionTicket,
        stage_resources: dict,
        schedule_ctx: ScheduleContext,
        search_start: datetime | None = None,
    ) -> None:
        """Schedule all stages for *production_ticket* sequentially."""
        if search_start is None:
            tomorrow = timezone.localdate() + timedelta(days=1)
            search_start = timezone.make_aware(
                datetime.combine(tomorrow, schedule_ctx.day_start)
            )

        last_session = None

        for stage, stage_info in (stage_resources or {}).items():
            start = (
                last_session.planned_end
                if last_session
                else search_start
            )
            last_session = cls._schedule_stage(
                production_ticket=production_ticket,
                stage=stage,
                stage_info=stage_info,
                schedule_ctx=schedule_ctx,
                search_start=start,
            )

    # -- per-stage scheduling -----------------------------------------------

    @classmethod
    def _schedule_stage(
        cls,
        production_ticket: ProductionTicket,
        stage: str,
        stage_info: dict,
        schedule_ctx: ScheduleContext,
        search_start: datetime,
    ) -> PlannedProductionSession | None:
        """Find a slot and create a session for one stage, or return None."""
        duration = timedelta(minutes=int(stage_info["total_time_min"]))

        resources, resource_type = cls._resolve_resources(stage_info)
        if not resources or resource_type is None:
            return None

        result = cls._find_slot(
            resources=resources,
            resource_type=resource_type,
            duration=duration,
            schedule_ctx=schedule_ctx,
            search_start=search_start,
        )

        if result is None:
            return None

        slot_start, resource_id = result

        return PlannedProductionSession.objects.create(
            production_ticket=production_ticket,
            production_stage=stage,
            planned_start=slot_start,
            planned_end=slot_start + duration,
            locked=False,
            source=PlannedProductionSessionSource.AUTOMATIC,
            primary_resource_id=resource_id,
        )

    # -- resource resolution ------------------------------------------------

    @staticmethod
    def _resolve_resources(
        stage_info: dict,
    ) -> tuple[list[ProductionResource], MachineType | EmployeeType | None]:
        """Look up the resource type and its concrete resources for a stage."""
        machines = stage_info.get("machine_types", [])
        employees = stage_info.get("employee_types", [])

        if machines:
            resource_type = MachineType.objects.filter(
                pk=machines[0]["machine_type_id"]
            ).first()
        elif employees:
            resource_type = EmployeeType.objects.filter(
                pk=employees[0]["employee_type_id"]
            ).first()
        else:
            return [], None

        if not resource_type:
            return [], None

        return get_resources(resource_type), resource_type

    # -- slot finder --------------------------------------------------------

    @classmethod
    def _find_slot(
        cls,
        resources: list[ProductionResource],
        resource_type: MachineType | EmployeeType,
        duration: timedelta,
        schedule_ctx: ScheduleContext,
        search_start: datetime,
    ) -> tuple[datetime, int] | None:
        """
        Walk forward in time until an available (start_time, resource_pk) pair
        is found, or give up after *max_days_ahead* days.

        Returns None if no slot could be found.
        """
        cursor = search_start
        days_ahead = 0

        while True:
            end_of_day = timezone.make_aware(
                datetime.combine(cursor.date(), schedule_ctx.day_end)
            )

            # 1. Check daily capacity
            capacity_available = cls._resources_with_capacity(
                resources, resource_type, cursor.date(), duration,
            )

            # 2. Check if the slot fits within the working day
            if cursor + duration > end_of_day or not capacity_available:
                days_ahead += 1
                if days_ahead > schedule_ctx.max_days_ahead:
                    return None
                cursor = timezone.make_aware(
                    datetime.combine(
                        cursor.date() + timedelta(days=1),
                        schedule_ctx.day_start,
                    )
                )
                continue

            # 3. Check for time-overlap conflicts
            conflicts = (
                PlannedProductionSession.objects
                .filter(
                    planned_start__lt=cursor + duration,
                    planned_end__gt=cursor,
                    primary_resource__in=resources,
                )
                .order_by("-planned_end")
            )
            used_resource_ids = set(
                conflicts.values_list("primary_resource_id", flat=True).distinct()
            )

            available = capacity_available - used_resource_ids

            if not available:
                # Jump past the latest conflicting session
                cursor = conflicts.first().planned_end
                continue

            return cursor, next(iter(available))

    # -- capacity check -----------------------------------------------------

    @staticmethod
    def _resources_with_capacity(
        resources: list[ProductionResource],
        resource_type: MachineType | EmployeeType,
        day,
        duration: timedelta,
    ) -> set:
        """
        Return the set of resource PKs that still have room for *duration*
        on the given day. For employees, all are returned (no daily cap).
        """
        if isinstance(resource_type, MachineType):
            return possible_resources_for_stage(resources, day, duration)
        return {res.pk for res in resources}
