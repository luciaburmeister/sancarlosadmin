from django.utils import timezone


def sync_configuration_planning_status(ticket):
    """Mirrors the existence of planned sessions onto the ticket's configuration.
    With several pieces it is planned once every active piece's ticket is."""
    from configurator.models import Configuration, ConfigurationPlanningStatus
    from production.models import ProductionTicket, ProductionTicketStatus

    config = ticket.source_object
    if not isinstance(config, Configuration):
        return

    unit_tickets = ProductionTicket.objects.filter(
        content_type_id=ticket.content_type_id, object_id=ticket.object_id,
    ).exclude(status=ProductionTicketStatus.CANCELLED)
    planned = ticket.planned_sessions.exists() and not unit_tickets.filter(
        planned_sessions__isnull=True,
    ).exists()
    new_status = (
        ConfigurationPlanningStatus.PLANNED
        if planned
        else ConfigurationPlanningStatus.UNPLANNED
    )
    # .exclude() keeps planned_at stable when the status doesn't actually change.
    changed = Configuration.objects.filter(pk=config.pk).exclude(
        planning_status=new_status
    ).update(
        planning_status=new_status,
        planned_at=timezone.now() if planned else None,
    )
    if changed:
        # .update() bypasses signals — refresh the quote's status manually.
        from sales.services.quote_production_status import (
            recompute_quote_production_status,
        )

        quote_id = (
            Configuration.objects.filter(pk=config.pk)
            .values_list("quote_id", flat=True)
            .first()
        )
        recompute_quote_production_status(quote_id)


class SessionEmployeeAssignmentSync:
    """Adds a planned/worked session's employee to its items' assigned_employees
    roster (never removes) so the task shows on their list right away."""

    def __init__(self, session):
        self.session = session

    def apply(self) -> None:
        from company.models import Employee
        from production.models import ProductionItem

        session = self.session
        if not session.production_ticket_id or not session.primary_resource_id:
            return

        employee = session.primary_resource.target
        if not isinstance(employee, Employee):
            return

        item_ids = session.production_ticket.item_ids_for_stage(session.production_stage)
        if not item_ids:
            return

        for item in ProductionItem.objects.filter(id__in=item_ids):
            item.assigned_employees.add(employee)


def sync_session_employee_assignment(session):
    SessionEmployeeAssignmentSync(session).apply()
