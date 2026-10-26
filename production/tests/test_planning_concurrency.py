from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from threading import Barrier

from django.db import close_old_connections, connections
from django.test import TransactionTestCase
from django.utils import timezone

from production.models import PlannedProductionSession
from production.services.production_planning import ProductionPlanningService
from production.tests.factories import (
    STAGE_CUT, add_schedule, make_company, make_employee, make_machine, make_ticket, resource_for,
)


class UnattendedMachineConcurrencyTest(TransactionTestCase):
    def test_two_planners_cannot_book_the_last_place(self):
        company = make_company()
        add_schedule(company)
        oven = resource_for(make_machine(
            company, name="Oven", stages=[STAGE_CUT], needs_human=False, parallel_capacity=1,
        ))
        tickets = [make_ticket(
            company, stages=[STAGE_CUT], stage_minutes={STAGE_CUT: 60},
            machine_qty=1, employee_qty=0,
        ) for _ in range(2)]
        start = timezone.make_aware(datetime(2026, 9, 14, 9))
        ready = Barrier(2)

        def book(ticket_id):
            close_old_connections()
            try:
                ready.wait(timeout=10)
                return ProductionPlanningService.plan_ticket_stage_custom(
                    ticket_id, start_at=start,
                    stage={"stage": STAGE_CUT, "duration_minutes": 60, "resource_ids": [oven.pk]},
                )
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=2) as workers:
            results = list(workers.map(book, [ticket.pk for ticket in tickets]))

        self.assertEqual(sorted(result.ok for result in results), [False, True])
        self.assertTrue(next(result for result in results if not result.ok).blocked)
        self.assertEqual(PlannedProductionSession.objects.count(), 1)


class OperatorMachineSelectionConcurrencyTest(TransactionTestCase):
    def test_concurrent_operator_drops_require_confirmation_to_share_a_full_machine(self):
        company = make_company()
        add_schedule(company)
        resource_for(make_machine(company, name="Cutter", stages=[STAGE_CUT]))
        operators = [
            resource_for(make_employee(company, first_name=name))
            for name in ("Ana", "Luis")
        ]
        tickets = [make_ticket(
            company, stages=[STAGE_CUT], stage_minutes={STAGE_CUT: 60},
            machine_qty=1, employee_qty=1,
        ) for _ in range(2)]
        start = timezone.make_aware(datetime(2026, 9, 14, 9))
        ready = Barrier(2)

        def book(pair):
            ticket, operator = pair
            close_old_connections()
            try:
                ready.wait(timeout=10)
                return ProductionPlanningService.plan_ticket_stage_custom(
                    ticket.pk, start_at=start,
                    stage={"stage": STAGE_CUT, "duration_minutes": 60, "resource_ids": [operator.pk]},
                    assign_machines=True,
                )
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=2) as workers:
            results = list(workers.map(book, zip(tickets, operators)))

        self.assertEqual(sorted(result.ok for result in results), [False, True])
        rejected = next(result for result in results if not result.ok)
        self.assertFalse(rejected.blocked)
        self.assertTrue(rejected.requires_confirmation)
        self.assertEqual(PlannedProductionSession.objects.count(), 1)
