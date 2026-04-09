from django.db import transaction

from production.models import PlannedProductionSession


class ScheduleProposalApplier:
    @transaction.atomic
    def apply(self, proposal):
        configuration_ids = proposal.selected_configuration_ids

        # Remove previous planned sessions for selected configurations
        PlannedProductionSession.objects.filter(
            configuration_id__in=configuration_ids
        ).delete()

        created = []

        for assignment in proposal.assignments:
            session = PlannedProductionSession.objects.create(
                configuration_id=assignment.configuration_id,
                configuration_item_id=assignment.configuration_item_id,
                stage_name=assignment.stage_name,
                production_resource_id=assignment.resource_id,
                planned_start=assignment.start,
                planned_end=assignment.end,
                metadata={
                    "copy_index": assignment.copy_index,
                    "slot_count": assignment.slot_count,
                    "minutes": assignment.minutes,
                },
            )
            created.append(session)

        return created