from configurator.models import Configuration
from datetime import datetime, date, time, timedelta
from decimal import Decimal, InvalidOperation
from django.core.exceptions import ValidationError
from configurator.utils.evaluator import safe_eval

from production.models import PlannedProductionSession, ProductionResource
from django.contrib.contenttypes.models import ContentType
from django.utils import timezone
from collections import defaultdict
from company.models import Company, Machine, Employee


def times_every_n_minutes(start_time: time, end_time: time, ts: int) -> list[time]:
    current = datetime.combine(date.min, start_time)
    end_dt = datetime.combine(date.min, end_time)

    times = []
    while current < end_dt:
        times.append(current.time())
        current += timedelta(minutes=ts)

    return times


def days_with_weekday(start_date: date, end_date: date) -> dict[date, int]:
    days = {}
    current = start_date

    while current <= end_date:
        days[current] = current.weekday()  # Monday=0, Sunday=6
        current += timedelta(days=1)

    return days

def _coerce_to_non_negative(value, field_name: str) -> int:
    try:
        number = Decimal(str(value))
    except (TypeError, ValueError, InvalidOperation):
        raise ValidationError({field_name: f"{field_name} must evaluate to a number."})

    if number < 0:
        raise ValidationError({field_name: f"{field_name} cannot be negative."})

    return int(number)

def _resolve_stage_info(stage_info: dict, context: dict, *, stage: str, configuration) -> dict:
    """
    Turns a saved stage resource dict into plain integers.

    Resource quantities are derived from the machine_types / employee_types
    arrays stored in the JSON (e.g. [{"quantity": 1, "machine_type_id": 1}]).
    """
    resolved = {}
    local_context = dict(context)

    # Derive flat machine_qty / employee_qty from the nested arrays
    enriched = dict(stage_info)
    enriched.setdefault(
        "machine_qty",
        sum(mt.get("quantity", 0) for mt in stage_info.get("machine_types", [])),
    )
    enriched.setdefault(
        "employee_qty",
        sum(et.get("quantity", 0) for et in stage_info.get("employee_types", [])),
    )

    for field_name in (
        "machine_qty",
        "employee_qty",
        "prep_time_min",
        "work_time_min",
        "post_time_min",
    ):
        raw_value = enriched.get(field_name, 0)

        try:
            if isinstance(raw_value, (int, float, Decimal)):
                value = raw_value
            else:
                value = safe_eval(str(raw_value), local_context)
        except Exception as e:
            raise ValidationError(
                {
                    field_name: (
                        f"Could not evaluate {field_name} for "
                        f"{configuration} / {stage}: {e}"
                    )
                }
            )

        resolved[field_name] = _coerce_to_non_negative(value, field_name)

    resolved["total_time_min"] = (
        resolved["prep_time_min"]
        + resolved["work_time_min"]
        + resolved["post_time_min"]
    )

    return resolved

def _slot_indexed_company_slots(possible_days_weekday, company_times_by_weekday, companies):
        return {
            company: [
                (d, t)
                for d, wd in sorted(possible_days_weekday.items())
                for t in company_times_by_weekday[company].get(wd, [])
            ]
            for company in companies
        }

def optimization_dict_creation(
    start_date: date,
    end_date: date,
    configurations: list[Configuration],
    ts: int,
) -> tuple[
    dict[Company, list[tuple[date, time]]],
    dict[Configuration, Company],
    dict[Configuration, float],
    dict[Configuration, list[str]],
    dict[tuple[Configuration, str], list[tuple[ContentType, int]]],
    dict[tuple[Configuration, str, tuple[ContentType, int]], int],
    dict[tuple[Configuration, str], int],
    dict[tuple[ContentType, int], list[ProductionResource]],
    dict[ProductionResource, int],
    dict[ProductionResource, int],
    dict[tuple[ProductionResource, int], int],
    dict[tuple[ProductionResource, date], int],
]:
    """Creates dictionaries for the optimization problem, aggregated at config×stage level.

    Parameters
    ----------
    start_date : date
        Date from which the optimizer can assign slots
    end_date : date
        Date to which the optimizer can assign slots
    configurations : list[Configuration]
        List of configurations for which to assign production slots
    ts : int
        Length of the slots to which to assign stages

    Returns
    -------
    company_ordered_slots : dict[Company, list[tuple[date, time]]]
        Orderede list of time slots for each company
    configuration_company : dict[Configuration, Company]
        Map configurations to their company
    configuration_margin : dict[Configuration, float]
        Margin per configuration
    configuration_stages : dict[Configuration, list[str]]
        Ordered list of unique schedulable stages per configuration
    stage_resource_types : dict[tuple[Configuration, str], list[tuple[ContentType, int]]]
        Resource types required per (config, stage)
    stage_resource_type_quantity : dict[tuple[Configuration, str, tuple[ContentType, int]], int]
        Quantity of each resource type needed per (config, stage)
    stage_duration : dict[tuple[Configuration, str], int]
        Aggregated duration in minutes per (config, stage)
    resource_type_active_resources : dict[tuple[ContentType, int], list[ProductionResource]]
        Active production resources per resource type
    resource_parallel_capacity : dict[ProductionResource, int]
        Parallel capacity per resource
    resource_daily_capacity_minutes : dict[ProductionResource, int]
        Daily capacity in minutes per resource
    resource_slot_load : dict[tuple[ProductionResource, int], int]
        How many sessions are using a resource in a given slot, capped to its maximum capacity
    resource_day_load : dict[tuple[ProductionResource, date], int]
        How many minutes a resource has been used in a day, capped to its maximum capacity
    """
    # Assignable times
    possible_days_weekday = days_with_weekday(start_date, end_date)
    configuration_company = {}
    company_times_by_weekday = {}

    # Margins
    configuration_margin = {}

    # Config-level stage aggregation
    configuration_stages = {}
    stage_resource_types = {}
    stage_resource_type_quantity = {}
    stage_duration = {}
    resource_types = set()

    # Available resources
    resource_type_active_resources = {}
    active_resources = set()
    resource_parallel_capacity = {}
    resource_daily_capacity_minutes = {}

    # Existing sessions
    resource_slot_load = defaultdict(int)
    resource_day_load = defaultdict(int)

    machine_ct = ContentType.objects.get_for_model(Machine)
    employee_ct = ContentType.objects.get_for_model(Employee)

    for configuration in configurations:
        company = configuration.company
        configuration_company[configuration] = company

        # Create dictionary of open times per weekday
        if company not in company_times_by_weekday:
            company_times_by_weekday[company] = {}
            for slot in company.schedule_slots.all():
                company_times_by_weekday[company].setdefault(slot.weekday, [])
                company_times_by_weekday[company][slot.weekday].extend(
                    times_every_n_minutes(slot.start_time, slot.end_time, ts)
                )

        # Dictionary of margin per configuration
        configuration_margin[configuration] = float(
            configuration.total_price.amount - configuration.total_cost.amount
        )

        # Read stage data directly from the product template
        product_stage_order = configuration.product.production_stages or []

        config_context = configuration.get_pricing_context()

        config_stage_durations = {}       # stage -> total minutes
        config_stage_rt = {}              # stage -> {rt: max_qty}

        for item in configuration.items.select_related("component").filter(is_template_item=True):
            item_stage_resources = item.stage_resources or {}

            for stage in (item.production_stages or []):
                raw_stage_info = item_stage_resources.get(stage, {})

                if not raw_stage_info:
                    continue

                stage_info = _resolve_stage_info(
                    raw_stage_info,
                    config_context,
                    stage=stage,
                    configuration=configuration,
                )

                if not stage_info["total_time_min"]:
                    continue

                # Stage duration is per-stage (not per-item), take max across items
                config_stage_durations[stage] = max(
                    config_stage_durations.get(stage, 0), stage_info["total_time_min"]
                )

                # Union resource types, take max quantity
                if stage not in config_stage_rt:
                    config_stage_rt[stage] = {}

                # Resource quantities are stored as arrays of dicts:
                #   machine_types: [{"quantity": N, "machine_type_id": X}, ...]
                #   employee_types: [{"quantity": N, "employee_type_id": X}, ...]
                machine_qty = sum(
                    mt.get("quantity", 0)
                    for mt in raw_stage_info.get("machine_types", [])
                )
                if machine_qty > 0:
                    rt = (machine_ct, stage)
                    existing_qty = config_stage_rt[stage].get(rt, 0)
                    config_stage_rt[stage][rt] = max(existing_qty, machine_qty)

                employee_qty = sum(
                    et.get("quantity", 0)
                    for et in raw_stage_info.get("employee_types", [])
                )
                if employee_qty > 0:
                    rt = (employee_ct, stage)
                    existing_qty = config_stage_rt[stage].get(rt, 0)
                    config_stage_rt[stage][rt] = max(existing_qty, employee_qty)

        # Build ordered stage list following product stage order
        ordered_stages = [s for s in product_stage_order if s in config_stage_durations]
        configuration_stages[configuration] = ordered_stages

        for stage in ordered_stages:
            stage_duration[(configuration, stage)] = config_stage_durations[stage]
            stage_resource_types[(configuration, stage)] = list(config_stage_rt.get(stage, {}).keys())

            for rt, qty in config_stage_rt.get(stage, {}).items():
                stage_resource_type_quantity[(configuration, stage, rt)] = qty

            resource_types.update(stage_resource_types[(configuration, stage)])

    # Dictionary of active resources per resource type
    for content_type, resource_type in resource_types:
        if content_type == machine_ct:
            eligible_machine_ids = Machine.objects.filter(
                is_active=True,
                production_stages__contains=[resource_type],
            ).values_list("id", flat=True)

            resources = ProductionResource.objects.filter(
                content_type=machine_ct,
                object_id__in=eligible_machine_ids,
                is_active=True,
            )
        elif content_type == employee_ct:
            eligible_employee_ids = Employee.objects.filter(
                is_active=True,
                can_work_on_production=True,
                production_stages__contains=[resource_type],
            ).values_list("id", flat=True)

            resources = ProductionResource.objects.filter(
                content_type=employee_ct,
                object_id__in=eligible_employee_ids,
                is_active=True,
            )
        else:
            resources = []

        resource_type_active_resources[(content_type, resource_type)] = list(resources)
        active_resources.update(resources)

    for resource in active_resources:
        resource_parallel_capacity[resource] = resource.parallel_capacity
        resource_daily_capacity_minutes[resource] = resource.daily_capacity_minutes

    # Company ordered slots
    company_ordered_slots = _slot_indexed_company_slots(
            possible_days_weekday, company_times_by_weekday,
            {configuration_company[c] for c in configurations},
        )

    # Used sessions
    sessions = PlannedProductionSession.objects.filter(
        primary_resource__in=active_resources,
        planned_start__date__lte=end_date,
        planned_end__date__gte=start_date,
    )

    for session in sessions:
        resource = session.primary_resource
        company = resource.company

        slot_cap = resource_parallel_capacity.get(resource, 0)
        day_cap = resource_daily_capacity_minutes.get(resource, 0)

        for pos, (day, slot_time) in enumerate(company_ordered_slots[company]):
            slot_start = timezone.make_aware(datetime.combine(day, slot_time))
            slot_end = slot_start + timedelta(minutes=ts)

            if session.planned_start < slot_end and session.planned_end > slot_start:
                current_slot_load = resource_slot_load[(resource, pos)]
                resource_slot_load[(resource, pos)] = min(slot_cap, current_slot_load + 1)

                overlap_start = max(session.planned_start, slot_start)
                overlap_end = min(session.planned_end, slot_end)
                overlap_minutes = max(0, int((overlap_end - overlap_start).total_seconds() // 60))
                current_day_load = resource_day_load[(resource, day)]
                resource_day_load[(resource, day)] = min(day_cap, current_day_load + overlap_minutes)

    return (
        company_ordered_slots,
        configuration_company,
        configuration_margin,
        configuration_stages,
        stage_resource_types,
        stage_resource_type_quantity,
        stage_duration,
        resource_type_active_resources,
        resource_parallel_capacity,
        resource_daily_capacity_minutes,
        resource_slot_load,
        resource_day_load
    )
