from django.db import transaction

from production.models import PlannedProductionSession
from production.services.configuration_to_production import ConfigurationToProductionService


class ScheduleProposalApplier:
    @transaction.atomic
    def apply(self, proposal):
        configuration_ids = proposal.selected_configuration_ids

        # Create tickets + items ONLY for selected configurations
        tickets = {
            c_id: ConfigurationToProductionService.create_ticket_by_id(c_id)
            for c_id in configuration_ids
        }

        # Build mapping: configuration_item_id → production_item
        production_items = {}
        for ticket in tickets.values():
            for pi in ticket.items.all():
                config_item_id = pi.data.get("configuration_item_id")
                if config_item_id:
                    production_items[config_item_id] = pi

        # Delete previous sessions for those items
        PlannedProductionSession.objects.filter(
            production_item__in=production_items.values()
        ).delete()

        created = []

        # Create sessions
        for assignment in proposal.assignments:
            production_item = production_items.get(assignment.configuration_item_id)

            if not production_item:
                continue  # or raise error if you want strict mode

            session = PlannedProductionSession.objects.create(
                production_item=production_item,
                production_stage=assignment.stage_name,
                planned_start=assignment.start,
                planned_end=assignment.end,
                primary_resource_id=assignment.resource_id,
            )
            created.append(session)

        return created