"""Shared builders for production tests.

Every planning test needs a company with a schedule, machines, employees and a
ticket wired through Configuration; hand-rolling that in each module is how a
suite rots. Grow this file as new phases need more objects rather than copying
setup between tests.
"""

from __future__ import annotations

from datetime import date, time

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.contrib.contenttypes.models import ContentType
from djmoney.money import Money

from company.models import Company, CompanyScheduleSlot, CompanyUser, Employee, Machine, WeekdayChoices
from configurator.models import Configuration, ConfigurationItem, ProductTemplate
from materials.models import Component
from production.models import (
    PlannedProductionSession,
    ProductionItem,
    ProductionItemSession,
    ProductionItemSessionStatus,
    ProductionItemStatus,
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


def make_product(company: Company, *, name=None, stages=None) -> ProductTemplate:
    """A ProductTemplate declaring *stages* (defaults to DEFAULT_STAGES)."""
    return ProductTemplate.objects.create(
        name=name or f"Producto {ProductTemplate.objects.count() + 1}",
        company=company,
        production_stages=list(stages if stages is not None else DEFAULT_STAGES),
    )


def make_ticket(
    company: Company,
    *,
    stages=None,
    product=None,
    configuration=None,
    bom=True,
    stage_minutes=None,
    machine_qty=0,
    employee_qty=1,
    **ticket_kwargs,
) -> ProductionTicket:
    """A ticket whose configuration carries per-stage durations and quantities.

    ``stage_minutes`` maps a stage key to its total minutes; when given, a
    template ConfigurationItem is created holding the ``stage_resources``
    snapshot the planner reads. Quantities go in the nested machine_types /
    employee_types arrays, which is the shape production code actually stores.
    ``bom=False`` skips that item (shop-floor tests that never plan), and
    ``configuration`` reuses an existing one instead of creating it.
    """
    stage_list = list(stages if stages is not None else DEFAULT_STAGES)
    if configuration is None:
        product = product or make_product(company, stages=stage_list)
        configuration = Configuration.objects.create(company=company, product=product)
    if bom:
        _add_bom_item(configuration, stage_list, stage_minutes, machine_qty, employee_qty)

    return ProductionTicket.objects.create(
        company=company,
        content_type=ContentType.objects.get_for_model(Configuration),
        object_id=configuration.pk,
        **ticket_kwargs,
    )


def _add_bom_item(configuration, stage_list, stage_minutes, machine_qty, employee_qty):
    """The template ConfigurationItem carrying the planner's stage snapshot."""
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


def make_item(ticket: ProductionTicket, *, stages=None, status=ProductionItemStatus.PENDING, planning=None, **kwargs) -> ProductionItem:
    """A ProductionItem with every stage at *status* (or the explicit *planning*)."""
    stage_list = list(stages if stages is not None else DEFAULT_STAGES)
    if planning is None:
        planning = {
            stage: {"status": status, "started_at": None, "completed_at": None}
            for stage in stage_list
        }
    return ProductionItem.objects.create(
        ticket=ticket,
        company=ticket.company,
        planning=planning,
        production_stages_order=kwargs.pop("production_stages_order", stage_list),
        **kwargs,
    )


def make_item_session(item: ProductionItem, *, employee, start, end=None, stage=STAGE_CUT, status=ProductionItemSessionStatus.DONE, **kwargs) -> ProductionItemSession:
    """A worked (shop-floor) session on *item*, created by *employee*."""
    return ProductionItemSession.objects.create(
        production_item=item,
        company=item.company,
        production_stage=stage,
        status=status,
        start_time=start,
        end_time=end,
        created_by=employee,
        **kwargs,
    )


def make_staff_user(*, username, companies=(), perms=()):
    """A staff user in *companies* holding the given production permission codenames."""
    user = get_user_model().objects.create_user(username=username, password="x", is_staff=True)
    for company in companies:
        CompanyUser.objects.create(user=user, company=company, is_active=True)
    if perms:
        found = list(Permission.objects.filter(codename__in=perms, content_type__app_label="production"))
        missing = set(perms) - {p.codename for p in found}
        if missing:
            raise ValueError(f"Unknown production permission codenames: {sorted(missing)}")
        user.user_permissions.set(found)
    return user


def make_staff_employee(company: Company, *, username, perms=(), stages=None, **kwargs) -> Employee:
    """A production Employee whose staff user belongs to *company* with *perms*."""
    user = make_staff_user(username=username, companies=[company], perms=perms)
    return make_employee(company, user=user, first_name=username, last_name="Test", stages=stages, **kwargs)


def make_scheduling_world(*, stage=STAGE_CUT, slot_end=time(10, 0)):
    """Company + one Monday 08:00-*slot_end* slot + product + component, for scheduler runs.

    Returns ``(company, product, component)``; add machines with make_machine.
    """
    company = make_company(stages=[stage])
    add_schedule(company, weekdays=[WeekdayChoices.MONDAY], ranges=((time(8, 0), slot_end),))
    product = make_product(company, name="Ventana", stages=[stage])
    component = Component.objects.create(company=company, name="Perfil", unit_price_expression="0")
    return company, product, component


def machine_stage_resources(stages, *, work_time_min=60, **machine_type):
    """A stage_resources snapshot needing one machine (of *machine_type*) per stage."""
    return {
        stage: {
            "machine_types": [{"quantity": 1, **machine_type}],
            "employee_types": [],
            "prep_time_min": 0,
            "work_time_min": work_time_min,
            "post_time_min": 0,
        }
        for stage in stages
    }


def make_priced_configuration(*, product, component, name, price, cost, stages, stage_resources) -> Configuration:
    """A priced Configuration with one template BOM item holding *stage_resources*."""
    configuration = Configuration.objects.create(
        company=product.company,
        product=product,
        name=name,
        total_price=Money(price, "EUR"),
        total_cost=Money(cost, "EUR"),
    )
    ConfigurationItem.objects.create(
        configuration=configuration,
        component=component,
        unit_price=Money(0, "EUR"),
        is_template_item=True,
        production_stages=list(stages),
        stage_resources=stage_resources,
    )
    return configuration
