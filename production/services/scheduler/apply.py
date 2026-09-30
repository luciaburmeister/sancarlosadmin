from collections import defaultdict

from django.db import transaction

from production.models import PlannedProductionSession
from production.services.configuration_to_production import ConfigurationToProductionService
from production.services.session_resources import sync_session_resources


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

        # The solver emits one assignment per (configuration, stage, resource).
        # A stage needing a machine *and* an operator therefore arrives as two
        # assignments that must collapse into ONE session carrying both.
        grouped: dict[tuple, list] = defaultdict(list)
        for assignment in proposal.assignments:
            if assignment.configuration_id not in tickets:
                continue
            key = (
                assignment.configuration_id,
                assignment.stage_name,
                assignment.start,
                assignment.end,
            )
            grouped[key].append(assignment)

        # Sequence numbers follow each ticket's own chronological stage order.
        per_configuration: dict[int, list[tuple]] = defaultdict(list)
        for key in grouped:
            per_configuration[key[0]].append(key)
        for keys in per_configuration.values():
            keys.sort(key=lambda k: (k[2], k[1]))

        created = []

        for configuration_id, keys in per_configuration.items():
            ticket = tickets[configuration_id]

            for sequence, key in enumerate(keys):
                _, stage_name, start, end = key

                session = PlannedProductionSession.objects.create(
                    production_ticket=ticket,
                    company=ticket.company,
                    production_stage=stage_name,
                    sequence=sequence,
                    planned_start=start,
                    planned_end=end,
                )
                sync_session_resources(
                    session, [a.resource_id for a in grouped[key]]
                )
                created.append(session)

        return created
