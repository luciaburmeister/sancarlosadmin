from collections import defaultdict

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext as _

from configurator.models import Configuration
from production.models import PlannedProductionSession, ProductionResource, ProductionTicket
from production.services.configuration_to_production import ConfigurationToProductionService
from production.services.planning_conflicts import ConflictChecker, SessionCandidate, blocking
from production.services.production_planning import ProductionPlanningService
from production.services.session_resources import sync_session_resources


class ScheduleProposalApplier:
    @transaction.atomic
    def apply(self, proposal):
        configuration_ids = proposal.selected_configuration_ids
        configuration_companies = dict(
            Configuration.objects.filter(pk__in=configuration_ids).values_list("pk", "company_id")
        )
        if set(configuration_ids) != set(configuration_companies):
            raise ValidationError(_("That planning no longer exists. Reload the board."))
        resource_ids = {
            assignment.resource_id for assignment in proposal.assignments
            if assignment.configuration_id in configuration_companies
        }
        resources = ProductionResource.objects.filter(pk__in=resource_ids, is_active=True).in_bulk()
        for assignment in proposal.assignments:
            if assignment.configuration_id not in configuration_companies:
                continue
            resource = resources.get(assignment.resource_id)
            if (
                resource is None
                or resource.company_id != configuration_companies[assignment.configuration_id]
            ):
                raise ValidationError(_("A selected resource is unavailable or belongs to another company."))

        # Create tickets for selected configurations
        tickets = {
            c_id: ConfigurationToProductionService.create_ticket_by_id(c_id)
            for c_id in configuration_ids
        }

        list(ProductionTicket.objects.select_for_update().filter(
            pk__in=[ticket.pk for ticket in tickets.values()]
        ).order_by("pk"))
        existing_sessions = list(
            PlannedProductionSession.objects.select_for_update()
            .filter(production_ticket__in=tickets.values()).order_by("pk")
        )
        if any(session.locked for session in existing_sessions):
            raise ValidationError(_("This session is locked. Unlock it before moving it."))
        stage_keys = [(s.production_ticket_id, s.production_stage) for s in existing_sessions]
        if len(stage_keys) != len(set(stage_keys)):
            raise ValidationError(_("This ticket has split bookings. Remove them from the calendar before replacing its plan automatically."))
        ProductionPlanningService.lock_resources(resource_ids)

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
        checkers = {}

        for configuration_id, keys in per_configuration.items():
            ticket = tickets[configuration_id]
            if ticket.company_id not in checkers:
                checkers[ticket.company_id] = ConflictChecker(ticket.company)

            for sequence, key in enumerate(keys):
                _configuration_id, stage_name, start, end = key

                resource_ids = [assignment.resource_id for assignment in grouped[key]]
                candidate = SessionCandidate(
                    company_id=ticket.company_id,
                    ticket_id=ticket.pk,
                    stage=stage_name,
                    start=timezone.make_aware(start) if timezone.is_naive(start) else start,
                    end=timezone.make_aware(end) if timezone.is_naive(end) else end,
                    resource_ids=resource_ids,
                    sequence=sequence,
                )
                conflicts = blocking(checkers[ticket.company_id].check(candidate))
                if conflicts:
                    raise ValidationError([conflict.message for conflict in conflicts])

                session = PlannedProductionSession.objects.create(
                    production_ticket=ticket,
                    company=ticket.company,
                    production_stage=stage_name,
                    sequence=sequence,
                    planned_start=candidate.start,
                    planned_end=candidate.end,
                )
                sync_session_resources(session, resource_ids)
                created.append(session)

        return created
