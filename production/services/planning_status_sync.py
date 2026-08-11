from django.utils import timezone


def sync_configuration_planning_status(ticket):
    """Mirror the existence of planned sessions on the ticket's source
    configuration, so quotes can be filtered on a stored field instead of
    walking ticket -> sessions at query time."""
    from configurator.models import Configuration, ConfigurationPlanningStatus

    config = ticket.source_object
    if not isinstance(config, Configuration):
        return

    planned = ticket.planned_sessions.exists()
    new_status = (
        ConfigurationPlanningStatus.PLANNED
        if planned
        else ConfigurationPlanningStatus.UNPLANNED
    )
    # Filter on the current status in the query itself: the `config` instance
    # may be a stale GenericFK cache, and this also keeps planned_at stable
    # when extra sessions are added to an already-planned configuration.
    changed = Configuration.objects.filter(pk=config.pk).exclude(
        planning_status=new_status
    ).update(
        planning_status=new_status,
        planned_at=timezone.now() if planned else None,
    )
    if changed:
        # .update() bypasses signals, so refresh the quote's denormalized
        # production status here.
        from sales.services.quote_production_status import (
            recompute_quote_production_status,
        )

        quote_id = (
            Configuration.objects.filter(pk=config.pk)
            .values_list("quote_id", flat=True)
            .first()
        )
        recompute_quote_production_status(quote_id)
