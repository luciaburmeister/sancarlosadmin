from configurator.models import Configuration, ConfigurationItem
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
    dict[Configuration, list[ConfigurationItem]],
    dict[ConfigurationItem, list[str]],
    dict[tuple[ConfigurationItem, str], list[tuple[ContentType, int]]],
    dict[tuple[ConfigurationItem, str, tuple[ContentType, int]], int],
    dict[tuple[ConfigurationItem, str], int],
    dict[tuple[ContentType, int], list[ProductionResource]],
    dict[ProductionResource, int],
    dict[ProductionResource, int],
]:
    """Creates dictionaries used for variable, objective, and constraint creation for the optimization problem

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
        Dict of days between start_date and end_date and their corresponding weekday (0-> monday, 6-> sunday)
    configuration_company : dict[Configuration, Company]
        Dict mapping configurations to their company
    company_times_by_weekday : dict[Company, dict[int, list[time]]]
        Dict mapping companies to a dictionary of open times separated by ts minutes per weekday
    configuration_margin : dict[Configuration, float]
        Dict mapping configurations to their margin
    configuration_items : dict[Configuration, list[ConfigurationItem]]
        Dict mapping configurations to a list of their configuration items
    item_stages : dict[ConfigurationItem, list[str]]
        Dict mapping configuration items to a list of their stages
    stage_resource_types : dict[tuple[ConfigurationItem, str], list[tuple[ContentType, int]]]
        Dict mapping stages to a list of their required resource types [ContentType, type__pk]
    stage_resource_type_quantity : dict[tuple[ConfigurationItem, str, tuple[ContentType, int]], int]
        Dict mapping stages' resurce types to their needed quantity
    stage_duration : dict[tuple[ConfigurationItem, str], int]
        Dict mappint stages to their duration
    resource_type_active_resources : dict[tuple[ContentType, int], list[ProductionResource]]
        Dict mapping resource types to a list of the active production resources of that type
    resource_parallel_capacity : dict[ProductionResource, int]
        Dict mapping resources to their parallel capacity
    resource_daily_capacity_minutes : dict[ProductionResource, int]
        Dict mapping resources to their daily production capacity in minutes
    """
    # Assignable times
    possible_days_weekday = days_with_weekday(start_date, end_date)
    configuration_company = {}
    company_times_by_weekday = {}

    # Margins
    configuration_margin = {}

    # Items, stages, and needed resources
    configuration_items = {}
    item_stages = {}
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

        configuration_items[configuration] = []
        # Dictionarys of resource types per stage per item per configuration
        for item in configuration.items.select_related("component"):
            configuration_items[configuration].append(item)
            item_stages[item] = []
            for stage, stage_info in item.stage_resources.items():
                item_stages[item].append(stage)
                stage_resource_types[(item, stage)] = []
                for machine_type in stage_info.get("machine_types", []):
                    rt = (machine_ct, machine_type["machine_type_id"])
                    stage_resource_types[(item, stage)].append(rt)
                    stage_resource_type_quantity[(item, stage, rt)] = machine_type["quantity"]

                for employee_type in stage_info.get("employee_types", []):
                    rt = (employee_ct, employee_type["employee_type_id"])
                    stage_resource_types[(item, stage)].append(rt)
                    stage_resource_type_quantity[(item, stage, rt)] = employee_type["quantity"]
                
                resource_types.update(stage_resource_types[(item, stage)])
                stage_duration[(item, stage)] = stage_info["total_time_min"]

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
        configuration_items,
        item_stages,
        stage_resource_types,
        stage_resource_type_quantity,
        stage_duration,
        resource_type_active_resources,
        resource_parallel_capacity,
        resource_daily_capacity_minutes,
    )
