from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from threading import Barrier, Event

from django.db import close_old_connections, connections, transaction
from django.test import TransactionTestCase
from django.utils import timezone

from production.models import PlannedProductionSession, ProductionItem, ProductionItemSession, ProductionItemStatus
from production.services.stages_to_sessions import StagesToSessionsService
from production.services.production_planning import ProductionPlanningService
from production.tests.factories import (
    STAGE_CUT, add_schedule, make_company, make_employee, make_item, make_machine, make_session, make_ticket, resource_for,
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


class SplitStageConcurrencyTest(TransactionTestCase):
    def test_two_parts_cannot_be_moved_to_the_same_slot_on_different_operators(self):
        company = make_company()
        add_schedule(company)
        operators = [resource_for(make_employee(company, first_name=name)) for name in ("Ana", "Luis")]
        ticket = make_ticket(company, stages=[STAGE_CUT])
        start = timezone.make_aware(datetime(2026, 9, 14, 9))
        sessions = [make_session(
            ticket, start=start + timedelta(hours=index * 2),
            end=start + timedelta(hours=index * 2 + 1), resources=[operator],
        ) for index, operator in enumerate(operators)]
        ready = Barrier(2)

        def move(session):
            close_old_connections()
            try:
                ready.wait(timeout=10)
                return ProductionPlanningService.move_session(
                    session.pk, new_start=start + timedelta(hours=1), override=True,
                )
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=2) as workers:
            results = list(workers.map(move, sessions))

        self.assertEqual(sorted(result.ok for result in results), [False, True])
        self.assertTrue(next(result for result in results if not result.ok).blocked)
        self.assertEqual(PlannedProductionSession.objects.filter(
            production_ticket=ticket, planned_start=start + timedelta(hours=1),
        ).count(), 1)


class CalendarStageCompletionConcurrencyTest(TransactionTestCase):
    def test_waiting_worker_cannot_restart_a_stage_completed_from_the_calendar(self):
        company = make_company()
        ticket = make_ticket(company, stages=[STAGE_CUT])
        item = make_item(ticket, stages=[STAGE_CUT])
        worker = make_employee(company)
        supervisor = make_employee(company, first_name="Supervisor", is_production_supervisor=True)
        loaded, attempt = Event(), Event()
        proceed = Event()

        def start():
            close_old_connections()
            try:
                stale_item = ProductionItem.objects.get(pk=item.pk)
                loaded.set()
                if not proceed.wait(10):
                    raise TimeoutError("Completion did not release the worker")
                attempt.set()
                return StagesToSessionsService.start_activity(
                    stage_name=STAGE_CUT, items=[stale_item], employee=worker,
                )
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(start)
            try:
                self.assertTrue(loaded.wait(10))
                with transaction.atomic():
                    count = StagesToSessionsService.complete_ticket_stage(
                        ticket=ticket, stage_name=STAGE_CUT, employee=supervisor,
                    )
                    self.assertEqual(count, 1)
                    proceed.set()
                    self.assertTrue(attempt.wait(10))
                    self.assertFalse(future.done())
                self.assertEqual(future.result(timeout=10), 0)
            finally:
                proceed.set()
        item.refresh_from_db()
        self.assertEqual(item.planning[STAGE_CUT]["status"], ProductionItemStatus.DONE)
        self.assertFalse(ProductionItemSession.objects.exists())
