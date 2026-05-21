from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta

from django.contrib.contenttypes.models import ContentType
from django.db.models import DurationField, ExpressionWrapper, F, Sum
from django.utils import timezone

from company.models import Employee, Machine
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
        defaults={"is_active": True, "company": target.company},
        company=target.company,
    )
    return resource


def get_resources_for_stage(stage: str, stage_info: dict) -> dict[str, list[ProductionResource]]:
    """
    Return ProductionResource wrappers for all active machines/employees
    that can work on this stage.
    """
    machine_qty = int(stage_info.get("machine_qty", 0) or 0)
    employee_qty = int(stage_info.get("employee_qty", 0) or 0)

    machine_resources: list[ProductionResource] = []
    employee_resources: list[ProductionResource] = []

    if machine_qty > 0:
        machine_resources = [
            get_or_create_resource_for_target(machine)
            for machine in Machine.objects.filter(
                is_active=True,
                production_stages__contains=[stage],
            )
        ]
        machine_resources = [r for r in machine_resources if r is not None]

    if employee_qty > 0:
        employee_resources = [
            get_or_create_resource_for_target(employee)
            for employee in Employee.objects.filter(
                is_active=True,
                can_work_on_production=True,
                production_stages__contains=[stage],
            )
        ]
        employee_resources = [r for r in employee_resources if r is not None]

    return {
        "machines": machine_resources,
        "employees": employee_resources,
    }


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
    """

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

        last_end = None

        for stage, stage_info in (stage_resources or {}).items():
            start = last_end if last_end else search_start

            result = cls._schedule_stage(
                production_ticket=production_ticket,
                stage=stage,
                stage_info=stage_info,
                schedule_ctx=schedule_ctx,
                search_start=start,
            )

            if result is None:
                continue

            last_end = result

    @classmethod
    def _schedule_stage(
        cls,
        production_ticket: ProductionTicket,
        stage: str,
        stage_info: dict,
        schedule_ctx: ScheduleContext,
        search_start: datetime,
    ) -> datetime | None:
        """
        Find a slot and create sessions for one stage.

        Returns the stage end datetime if scheduled, else None.
        """
        duration = timedelta(minutes=int(stage_info["total_time_min"]))
        machine_qty = int(stage_info.get("machine_qty", 0) or 0)
        employee_qty = int(stage_info.get("employee_qty", 0) or 0)

        if machine_qty <= 0 and employee_qty <= 0:
            return None

        pools = get_resources_for_stage(stage, stage_info)
        machine_resources = pools["machines"]
        employee_resources = pools["employees"]

        if machine_qty > len(machine_resources) or employee_qty > len(employee_resources):
            return None

        result = cls._find_slot(
            machine_resources=machine_resources,
            employee_resources=employee_resources,
            machine_qty=machine_qty,
            employee_qty=employee_qty,
            duration=duration,
            schedule_ctx=schedule_ctx,
            search_start=search_start,
        )

        if result is None:
            return None

        slot_start, selected_machine_ids, selected_employee_ids = result
        slot_end = slot_start + duration

        for resource_id in selected_machine_ids + selected_employee_ids:
            PlannedProductionSession.objects.create(
                production_ticket=production_ticket,
                production_stage=stage,
                planned_start=slot_start,
                planned_end=slot_end,
                locked=False,
                source=PlannedProductionSessionSource.AUTOMATIC,
                primary_resource_id=resource_id,
            )

        return slot_end

    @classmethod
    def _find_slot(
        cls,
        machine_resources: list[ProductionResource],
        employee_resources: list[ProductionResource],
        machine_qty: int,
        employee_qty: int,
        duration: timedelta,
        schedule_ctx: ScheduleContext,
        search_start: datetime,
    ) -> tuple[datetime, list[int], list[int]] | None:
        """
        Walk forward in time until enough machines and employees are simultaneously
        available.

        Returns:
            (slot_start, machine_resource_ids, employee_resource_ids)
        """
        cursor = search_start
        days_ahead = 0

        while True:
            end_of_day = timezone.make_aware(
                datetime.combine(cursor.date(), schedule_ctx.day_end)
            )

            # First check if the slot fits within the workday
            if cursor + duration > end_of_day:
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

            # Machine availability
            available_machine_ids = possible_resources_for_stage(
                machine_resources,
                cursor.date(),
                duration,
            )

            # Employee availability: no daily capacity cap, only overlap checks
            available_employee_ids = {res.pk for res in employee_resources}

            if len(available_machine_ids) < machine_qty or len(available_employee_ids) < employee_qty:
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

            # Remove resources already busy in the time window
            conflicts = (
                PlannedProductionSession.objects.filter(
                    planned_start__lt=cursor + duration,
                    planned_end__gt=cursor,
                    primary_resource__in=machine_resources + employee_resources,
                )
                .order_by("-planned_end")
            )

            used_resource_ids = set(
                conflicts.values_list("primary_resource_id", flat=True).distinct()
            )

            free_machine_ids = sorted(available_machine_ids - used_resource_ids)
            free_employee_ids = sorted(available_employee_ids - used_resource_ids)

            if len(free_machine_ids) < machine_qty or len(free_employee_ids) < employee_qty:
                if conflicts.exists():
                    cursor = conflicts.first().planned_end
                    continue

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

            return (
                cursor,
                free_machine_ids[:machine_qty],
                free_employee_ids[:employee_qty],
            )