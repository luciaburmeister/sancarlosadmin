def compute_quote_production_status(quote_id):
    from configurator.models import ConfigurationPlanningStatus, ConfigurationStatus
    from configurator.models import Configuration
    from sales.models import QuoteProductionStatus

    rows = list(
        Configuration.objects.filter(quote_id=quote_id)
        .exclude(status=ConfigurationStatus.CANCELLED)
        .values_list("status", "planning_status")
    )
    if not rows:
        return QuoteProductionStatus.NO_PRODUCTION
    if any(
        status in (ConfigurationStatus.DRAFT, ConfigurationStatus.COMPLETED)
        for status, _ in rows
    ):
        return QuoteProductionStatus.NOT_CONVERTED
    if any(
        planning != ConfigurationPlanningStatus.PLANNED for _, planning in rows
    ):
        return QuoteProductionStatus.UNSCHEDULED
    return QuoteProductionStatus.SCHEDULED


def recompute_quote_production_status(quote_id):
    """Refresh the denormalized Quote.production_status from the quote's
    configurations. Called whenever a configuration or its planning changes."""
    from sales.models import Quote

    if not quote_id:
        return
    new_status = compute_quote_production_status(quote_id)
    Quote.objects.filter(pk=quote_id).exclude(production_status=new_status).update(
        production_status=new_status
    )
