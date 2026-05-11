from configurator.models import Configuration
from datetime import datetime, date, time, timedelta

from production.models import ProductionResource
from django.contrib.contenttypes.models import ContentType
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


def optimization_dict_creation(
    start_date: date,
    end_date: date,
    configurations: list[Configuration],
    ts: int,
) -> tuple[
    dict[date, int],
    dict[Configuration, Company],
    dict[Company, dict[int, list[time]]],
    dict[Configuration, float],
    dict[Configuration, list[str]],
    dict[tuple[Configuration, str], list[tuple[ContentType, int]]],
    dict[tuple[Configuration, str, tuple[ContentType, int]], int],
    dict[tuple[Configuration, str], int],
    dict[tuple[ContentType, int], list[ProductionResource]],
    dict[ProductionResource, int],
    dict[ProductionResource, int],
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
    possible_days_weekday : dict[date, int]
        Days between start_date and end_date with their weekday
    configuration_company : dict[Configuration, Company]
        Map configurations to their company
    company_times_by_weekday : dict[Company, dict[int, list[time]]]
        Open times separated by ts minutes per weekday per company
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

        # Aggregate stages across all items in this configuration
        # Use the product's stage ordering as the canonical order
        product_stage_order = configuration.product.production_stages or []

        # Collect per-stage aggregated data
        config_stage_durations = {}       # stage -> total minutes
        config_stage_rt = {}              # stage -> {rt: max_qty}

        for item in configuration.items.select_related("component").filter(is_template_item=True):
            item_stage_resources = item.stage_resources or {}

            for stage in (item.production_stages or []):
                stage_info = item_stage_resources.get(stage, {})

                if not stage_info or not stage_info.get("total_time_min"):
                    continue

                # Sum duration across items
                config_stage_durations[stage] = (
                    config_stage_durations.get(stage, 0) + stage_info["total_time_min"]
                )

                # Union resource types, take max quantity
                if stage not in config_stage_rt:
                    config_stage_rt[stage] = {}

                for machine_type in stage_info.get("machine_types", []):
                    rt = (machine_ct, machine_type["machine_type_id"])
                    existing_qty = config_stage_rt[stage].get(rt, 0)
                    config_stage_rt[stage][rt] = max(existing_qty, machine_type["quantity"])

                for employee_type in stage_info.get("employee_types", []):
                    rt = (employee_ct, employee_type["employee_type_id"])
                    existing_qty = config_stage_rt[stage].get(rt, 0)
                    config_stage_rt[stage][rt] = max(existing_qty, employee_type["quantity"])

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
            machine_ids = Machine.objects.filter(
                type_id=resource_type,
                is_active=True,
            ).values_list("id", flat=True)

            resources = ProductionResource.objects.filter(
                content_type=machine_ct,
                object_id__in=machine_ids,
                is_active=True,
            )
        elif content_type == employee_ct:
            employee_ids = Employee.objects.filter(
                type_id=resource_type,
                is_active=True,
            ).values_list("id", flat=True)

            resources = ProductionResource.objects.filter(
                content_type=employee_ct,
                object_id__in=employee_ids,
                is_active=True,
            )
        else:
            resources = []

        resource_type_active_resources[(content_type, resource_type)] = list(resources)
        active_resources.update(resources)

    for resource in active_resources:
        resource_parallel_capacity[resource] = resource.parallel_capacity
        resource_daily_capacity_minutes[resource] = resource.daily_capacity_minutes

    return (
        possible_days_weekday,
        configuration_company,
        company_times_by_weekday,
        configuration_margin,
        configuration_stages,
        stage_resource_types,
        stage_resource_type_quantity,
        stage_duration,
        resource_type_active_resources,
        resource_parallel_capacity,
        resource_daily_capacity_minutes,
    )
