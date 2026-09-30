from __future__ import annotations

from datetime import datetime

from django.test import TestCase
from django.utils import timezone

from production.models import (
    PlannedProductionSession,
    ProductionTicketStatus,
)
from production.services.ticket_cancel import cancel_production_ticket
from production.tests.factories import (
    STAGE_CUT,
    STAGE_PAINT,
    add_schedule,
    make_company,
    make_machine,
    make_session,
    make_ticket,
    resource_for,
)

MONDAY_0800 = datetime(2026, 9, 14, 8, 0)


class CancelTicketClearsTheCalendarTest(TestCase):
    """A cancelled ticket must stop occupying machine and operator time.

    Its stages resolve to neither done nor in progress, so sessions left behind
    would read as merely planned, go overdue once their end passed, and never
    be clearable -- the ticket refuses further mutation once cancelled.
    """

    @classmethod
    def setUpTestData(cls):
        cls.company = make_company()
        add_schedule(cls.company)
        cls.tz = timezone.get_current_timezone()
        cls.start = MONDAY_0800.replace(tzinfo=cls.tz)
        cls.machine = resource_for(make_machine(cls.company, name="CNC-01"))

    def _ticket_with_sessions(self, *, locked=False):
        ticket = make_ticket(self.company, stage_minutes={STAGE_CUT: 60})
        make_session(
            ticket,
            stage=STAGE_CUT,
            start=self.start,
            end=self.start.replace(hour=9),
            resources=[self.machine],
            sequence=0,
            locked=locked,
        )
        return ticket

    def test_cancelling_removes_the_tickets_planned_sessions(self):
        ticket = self._ticket_with_sessions()

        changed = cancel_production_ticket(ticket)

        self.assertTrue(changed)
        self.assertEqual(ticket.status, ProductionTicketStatus.CANCELLED)
        self.assertFalse(
            PlannedProductionSession.objects.filter(production_ticket=ticket).exists()
        )

    def test_another_tickets_sessions_are_left_alone(self):
        cancelled = self._ticket_with_sessions()
        survivor = make_ticket(self.company, stage_minutes={STAGE_PAINT: 60})
        make_session(
            survivor,
            stage=STAGE_PAINT,
            start=self.start.replace(hour=11),
            end=self.start.replace(hour=12),
            resources=[self.machine],
            sequence=0,
        )

        cancel_production_ticket(cancelled)

        self.assertEqual(
            PlannedProductionSession.objects.filter(
                production_ticket=survivor
            ).count(),
            1,
        )

    def test_a_locked_session_is_left_for_a_human(self):
        ticket = self._ticket_with_sessions(locked=True)

        cancel_production_ticket(ticket)

        self.assertTrue(
            PlannedProductionSession.objects.filter(production_ticket=ticket).exists()
        )

    def test_cancelling_an_already_cancelled_ticket_does_nothing(self):
        ticket = self._ticket_with_sessions()
        ticket.status = ProductionTicketStatus.CANCELLED
        ticket.save(update_fields=["status"])

        self.assertFalse(cancel_production_ticket(ticket))
        self.assertEqual(
            PlannedProductionSession.objects.filter(production_ticket=ticket).count(),
            1,
        )
