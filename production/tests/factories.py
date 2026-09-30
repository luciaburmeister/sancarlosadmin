"""Shared builders for production tests.

Every planning test needs a company with a schedule, machines, employees and a
ticket wired through Configuration; hand-rolling that in each module is how a
suite rots. Grow this file as new phases need more objects rather than copying
setup between tests.
"""

from __future__ import annotations

from datetime import date, time

from django.contrib.contenttypes.models import ContentType

from company.models import Company, CompanyScheduleSlot, Employee, Machine, WeekdayChoices
from configurator.models import Configuration, ConfigurationItem, ProductTemplate
from materials.models import Component
from production.models import (
    PlannedProductionSession,
    ProductionResource,
    ProductionTicket,
)
from production.services.session_resources import sync_session_resources

# Stage keys used across the planning tests.
STAGE_CUT = "corte"
STAGE_WELD = "soldadura"
STAGE_PAINT = "pintura"
DEFAULT_STAGES = [STAGE_CUT, STAGE_WELD, STAGE_PAINT]

WEEKDAYS = [
    WeekdayChoices.MONDAY,
    WeekdayChoices.TUESDAY,
    WeekdayChoices.WEDNESDAY,
    WeekdayChoices.THURSDAY,
    WeekdayChoices.FRIDAY,
]


def make_company(*, name="Test Co", stages=None, **kwargs) -> Company:
    return Company.objects.create(
        name=name,
        production_stages=list(stages if stages is not None else DEFAULT_STAGES),
        **kwargs,
    )


def add_schedule(
    company: Company,
    *,
    weekdays=None,
    ranges=((time(8, 0), time(14, 0)), (time(15, 0), time(18, 0))),
) -> list[CompanyScheduleSlot]:
    """Give *company* a weekly schedule. Defaults to a Spanish split shift."""
    slots = []
    for weekday in (WEEKDAYS if weekdays is None else weekdays):
        for start, end in ranges:
            slots.append(
                CompanyScheduleSlot.objects.create(
                    company=company, weekday=weekday, start_time=start, end_time=end,
                )
            )
    return slots


def make_employee(company: Company, *, first_name="Ana", last_name="Ruiz", stages=None, **kwargs) -> Employee:
    return Employee.objects.create(
        company=company,
        first_name=first_name,
        last_name=last_name,
        hired_at=date(2020, 1, 1),
        can_work_on_production=kwargs.pop("can_work_on_production", True),
        production_stages=list(stages if stages is not None else DEFAULT_STAGES),
        **kwargs,
    )


def make_machine(company: Company, *, name="CNC-01", code=None, stages=None, **kwargs) -> Machine:
    return Machine.objects.create(
        company=company,
        name=name,
        code=code or name.lower().replace(" ", "-"),
        production_stages=list(stages if stages is not None else DEFAULT_STAGES),
        daily_capacity_minutes=kwargs.pop("daily_capacity_minutes", 540),
        **kwargs,
    )


def resource_for(target) -> ProductionResource:
    """The ProductionResource wrapping an Employee or Machine.

    production.signals creates it on save, so this normally just fetches it.
    """
    content_type = ContentType.objects.get_for_model(type(target))
    resource, _ = ProductionResource.objects.get_or_create(
        content_type=content_type,
        object_id=target.pk,
        defaults={"company": target.company, "is_active": True},
    )
    return resource


def make_ticket(
    company: Company,
    *,
    stages=None,
    product=None,
    stage_minutes=None,
    machine_qty=0,
    employee_qty=1,
) -> ProductionTicket:
    """A ticket whose configuration carries per-stage durations and quantities.

    ``stage_minutes`` maps a stage key to its total minutes; when given, a
    template ConfigurationItem is created holding the ``stage_resources``
    snapshot the planner reads. Quantities go in the nested machine_types /
    employee_types arrays, which is the shape production code actually stores.
    """
    stage_list = list(stages if stages is not None else DEFAULT_STAGES)
    product = product or ProductTemplate.objects.create(
        name=f"Producto {ProductTemplate.objects.count() + 1}",
        company=company,
        production_stages=stage_list,
    )
    configuration = Configuration.objects.create(company=company, product=product)

    # Always a BOM item declaring the stages: a ticket's plannable stages come
    # from its items, not from the product, so a configuration without one is
    # unplannable everywhere -- generate_bom() never leaves one that way.
    component = Component.objects.create(
        name=f"Perfil {Component.objects.count() + 1}",
        unit_price_expression="1",
    )
    ConfigurationItem.objects.create(
        configuration=configuration,
        component=component,
        quantity=1,
        length=1,
        unit_price=0,
        is_template_item=True,
        production_stages=stage_list,
        stage_resources={
            stage: {
                "prep_time_min": 0,
                "work_time_min": minutes,
                "post_time_min": 0,
                "machine_types": [{"quantity": machine_qty}] if machine_qty else [],
                "employee_types": [{"quantity": employee_qty}] if employee_qty else [],
            }
            for stage, minutes in (stage_minutes or {}).items()
        },
    )

    return ProductionTicket.objects.create(
        company=company,
        content_type=ContentType.objects.get_for_model(Configuration),
        object_id=configuration.pk,
    )


def make_session(
    ticket: ProductionTicket,
    *,
    stage=STAGE_CUT,
    start,
    end,
    resources=(),
    sequence=0,
    **kwargs,
) -> PlannedProductionSession:
    """A planned session owning *resources* (ProductionResource instances)."""
    session = PlannedProductionSession.objects.create(
        production_ticket=ticket,
        company=ticket.company,
        production_stage=stage,
        sequence=sequence,
        planned_start=start,
        planned_end=end,
        **kwargs,
    )
    if resources:
        sync_session_resources(session, [r.pk for r in resources])
        session.refresh_from_db()
    return session
