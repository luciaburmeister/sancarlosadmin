from django.db import transaction

from production.models import PlannedProductionSession
from production.services.configuration_to_production import ConfigurationToProductionService


class ScheduleProposalApplier:
    @transaction.atomic
    def apply(self, proposal):
        configuration_ids = proposal.selected_configuration_ids

        # Create tickets for selected configurations
        tickets = {
            c_id: ConfigurationToProductionService.create_ticket_by_id(c_id)
            for c_id in configuration_ids
        }

        # Delete previous planned sessions for those tickets
        PlannedProductionSession.objects.filter(
            production_ticket__in=tickets.values()
        ).delete()

        created = []

        # Create sessions — one per (configuration, stage) assignment
        for assignment in proposal.assignments:
            ticket = tickets.get(assignment.configuration_id)

            if not ticket:
                continue

            session = PlannedProductionSession.objects.create(
                production_ticket=ticket,
                production_stage=assignment.stage_name,
                planned_start=assignment.start,
                planned_end=assignment.end,
                primary_resource_id=assignment.resource_id,
            )
            created.append(session)

        return created