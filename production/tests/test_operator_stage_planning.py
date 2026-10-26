from datetime import datetime, timedelta

from django.test import TestCase
from django.utils import timezone

from production.models import PlannedProductionSession
from production.services.planning_conflicts import ConflictCode
from production.services.production_planning import ProductionPlanningService
from production.tests.factories import (
    STAGE_CUT,
    STAGE_PAINT,
    add_schedule,
    make_company,
    make_employee,
    make_machine,
    make_session,
    make_ticket,
    resource_for,
)


class OperatorStagePlanningTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = make_company()
        add_schedule(cls.company)
        cls.start = timezone.make_aware(datetime(2026, 9, 14, 9))
        cls.operator = resource_for(make_employee(cls.company))
        cls.machine = resource_for(make_machine(cls.company))
        cls.ticket = make_ticket(
            cls.company, stages=[STAGE_CUT], stage_minutes={STAGE_CUT: 60},
            machine_qty=1, employee_qty=1,
        )

    def plan(self, *, ticket=None, operator=None, override=False):
        return ProductionPlanningService.plan_ticket_stage_custom(
            (ticket or self.ticket).pk,
            start_at=self.start,
            stage={
                "stage": STAGE_CUT,
                "duration_minutes": 60,
                "resource_ids": [(operator or self.operator).pk],
            },
            assign_machines=True,
            override=override,
        )

    def book_machine(self, machine, *, start=None, end=None):
        return make_session(
            make_ticket(self.company), resources=[machine],
            start=start or self.start,
            end=end or self.start + timedelta(hours=1),
        )

    def assert_assigned(self, result, *resources):
        self.assertTrue(result.ok, result.conflicts)
        self.assertEqual(
            set(result.sessions[0].resources.values_list("resource_id", flat=True)),
            {self.operator.pk, *(resource.pk for resource in resources)},
        )
        self.assertEqual(result.sessions[0].primary_resource_id, resources[0].pk)

    def test_assigns_the_only_machine(self):
        self.assert_assigned(self.plan(), self.machine)

    def test_unconfigured_stage_infers_one_machine_from_stage_assignments(self):
        ticket = make_ticket(self.company, stages=[STAGE_CUT])

        self.assert_assigned(self.plan(ticket=ticket), self.machine)

    def test_time_only_configuration_does_not_disable_machine_inference(self):
        item = self.ticket.source_object.items.get(is_template_item=True)
        item.stage_resources = {STAGE_CUT: {"work_time_min": 60}}
        item.save(update_fields=["stage_resources"])

        self.assert_assigned(self.plan(), self.machine)

    def test_explicit_zero_machine_requirement_remains_operator_only(self):
        ticket = make_ticket(
            self.company, stages=[STAGE_CUT], stage_minutes={STAGE_CUT: 60},
            machine_qty=0, employee_qty=1,
        )

        result = self.plan(ticket=ticket)

        self.assertTrue(result.ok, result.conflicts)
        self.assertEqual(
            list(result.sessions[0].resources.values_list("resource_id", flat=True)),
            [self.operator.pk],
        )

    def test_unconfigured_stage_without_company_machines_remains_operator_only(self):
        machine = self.machine.target
        machine.production_stages = [STAGE_PAINT]
        machine.save(update_fields=["production_stages"])
        resource_for(make_machine(make_company(name="Other"), name="Foreign cutter"))
        ticket = make_ticket(self.company, stages=[STAGE_CUT])

        result = self.plan(ticket=ticket)

        self.assertTrue(result.ok, result.conflicts)
        self.assertEqual(
            list(result.sessions[0].resources.values_list("resource_id", flat=True)),
            [self.operator.pk],
        )

    def test_unconfigured_stage_requires_confirmation_for_a_busy_machine(self):
        self.book_machine(self.machine)
        ticket = make_ticket(self.company, stages=[STAGE_CUT])

        result = self.plan(ticket=ticket)

        self.assertFalse(result.blocked)
        self.assertTrue(result.requires_confirmation)
        self.assertIn(ConflictCode.RESOURCE_OVERLAP, {c.code for c in result.conflicts})
        self.assertFalse(PlannedProductionSession.objects.filter(production_ticket=ticket).exists())
        self.assert_assigned(self.plan(ticket=ticket, override=True), self.machine)

    def test_unconfigured_stage_can_confirm_without_an_inactive_machine(self):
        machine = self.machine.target
        machine.is_active = False
        machine.save(update_fields=["is_active"])
        ticket = make_ticket(self.company, stages=[STAGE_CUT])

        result = self.plan(ticket=ticket)

        self.assertFalse(result.blocked)
        self.assertTrue(result.requires_confirmation)
        self.assertFalse(PlannedProductionSession.objects.filter(production_ticket=ticket).exists())
        confirmed = self.plan(ticket=ticket, override=True)
        self.assertTrue(confirmed.ok, confirmed.conflicts)
        self.assertEqual(
            list(confirmed.sessions[0].resources.values_list("resource_id", flat=True)),
            [self.operator.pk],
        )

    def test_explicit_nested_machine_and_operator_are_kept(self):
        ticket = make_ticket(self.company, stages=[STAGE_CUT])

        result = ProductionPlanningService.plan_ticket_stage_custom(
            ticket.pk, start_at=self.start,
            stage={
                "stage": STAGE_CUT, "duration_minutes": 60,
                "resource_ids": [self.machine.pk, self.operator.pk],
            },
            assign_machines=True,
        )

        self.assert_assigned(result, self.machine)

    def test_chooses_a_free_machine_for_the_entire_interval(self):
        spare = resource_for(make_machine(self.company, name="Spare"))
        self.book_machine(self.machine, start=self.start + timedelta(minutes=30))

        self.assert_assigned(self.plan(), spare)

    def test_adjacent_booking_does_not_make_a_machine_unavailable(self):
        self.book_machine(
            self.machine, start=self.start - timedelta(hours=1), end=self.start,
        )

        self.assert_assigned(self.plan(), self.machine)

    def test_selects_two_free_machines_when_required(self):
        spare = resource_for(make_machine(self.company, name="Spare"))
        second = resource_for(make_machine(self.company, name="Second"))
        self.book_machine(self.machine)
        ticket = make_ticket(
            self.company, stages=[STAGE_CUT], stage_minutes={STAGE_CUT: 60},
            machine_qty=2, employee_qty=1,
        )

        self.assert_assigned(self.plan(ticket=ticket), spare, second)

    def test_two_machines_are_assigned_after_confirming_a_busy_machine(self):
        spare = resource_for(make_machine(self.company, name="Spare"))
        self.book_machine(self.machine)
        ticket = make_ticket(
            self.company, stages=[STAGE_CUT], stage_minutes={STAGE_CUT: 60},
            machine_qty=2, employee_qty=1,
        )

        result = self.plan(ticket=ticket)

        self.assertFalse(result.ok)
        self.assertFalse(result.blocked)
        self.assertTrue(result.requires_confirmation)
        self.assertFalse(PlannedProductionSession.objects.filter(production_ticket=ticket).exists())
        self.assertFalse(spare.planned_assignments.exists())
        self.assert_assigned(self.plan(ticket=ticket, override=True), spare, self.machine)

    def test_machine_shortfall_can_be_confirmed_with_partial_assignment(self):
        ticket = make_ticket(
            self.company, stages=[STAGE_CUT], stage_minutes={STAGE_CUT: 60},
            machine_qty=2, employee_qty=1,
        )

        result = self.plan(ticket=ticket)

        self.assertFalse(result.blocked)
        self.assertTrue(result.requires_confirmation)
        self.assertIn(ConflictCode.REQUIREMENT_UNMET, {c.code for c in result.conflicts})
        self.assertFalse(PlannedProductionSession.objects.filter(production_ticket=ticket).exists())
        self.assert_assigned(self.plan(ticket=ticket, override=True), self.machine)

    def test_missing_all_machines_can_be_confirmed_as_operator_only(self):
        machine = self.machine.target
        machine.production_stages = [STAGE_PAINT]
        machine.save(update_fields=["production_stages"])

        result = self.plan()

        self.assertFalse(result.blocked)
        self.assertTrue(result.requires_confirmation)
        self.assertFalse(PlannedProductionSession.objects.filter(production_ticket=self.ticket).exists())
        confirmed = self.plan(override=True)
        self.assertTrue(confirmed.ok, confirmed.conflicts)
        self.assertEqual(
            list(confirmed.sessions[0].resources.values_list("resource_id", flat=True)),
            [self.operator.pk],
        )

    def test_no_free_machine_does_not_create_a_booking(self):
        self.book_machine(self.machine)

        result = self.plan()

        self.assertFalse(result.blocked)
        self.assertTrue(result.requires_confirmation)
        self.assertFalse(PlannedProductionSession.objects.filter(production_ticket=self.ticket).exists())

    def test_skips_a_machine_without_enough_daily_capacity(self):
        machine = self.machine.target
        machine.daily_capacity_minutes = 60
        machine.save(update_fields=["daily_capacity_minutes"])
        self.book_machine(
            self.machine, start=self.start - timedelta(hours=1), end=self.start,
        )
        spare = resource_for(make_machine(self.company, name="Spare"))

        self.assert_assigned(self.plan(), spare)

    def test_daily_capacity_can_be_confirmed_when_no_free_alternative_exists(self):
        machine = self.machine.target
        machine.daily_capacity_minutes = 30
        machine.save(update_fields=["daily_capacity_minutes"])

        result = self.plan()

        self.assertFalse(result.ok)
        self.assertFalse(result.blocked)
        self.assertTrue(result.requires_confirmation)
        self.assertIn(ConflictCode.DAILY_CAPACITY_EXCEEDED, {c.code for c in result.conflicts})
        self.assertFalse(PlannedProductionSession.objects.filter(production_ticket=self.ticket).exists())
        self.assert_assigned(self.plan(override=True), self.machine)

    def test_full_unattended_machine_cannot_be_confirmed(self):
        machine = self.machine.target
        machine.needs_human = False
        machine.save(update_fields=["needs_human"])
        self.book_machine(self.machine)

        for override in (False, True):
            with self.subTest(override=override):
                result = self.plan(override=override)

                self.assertTrue(result.blocked)
                self.assertFalse(result.requires_confirmation)
                self.assertIn(ConflictCode.RESOURCE_OVERLAP, {c.code for c in result.conflicts})
                self.assertFalse(PlannedProductionSession.objects.filter(production_ticket=self.ticket).exists())

    def test_prefers_confirmable_machine_over_full_unattended_machine(self):
        machine = self.machine.target
        machine.needs_human = False
        machine.save(update_fields=["needs_human"])
        spare = resource_for(make_machine(self.company, name="Spare"))
        self.book_machine(self.machine)
        self.book_machine(spare)

        result = self.plan()

        self.assertFalse(result.blocked)
        self.assertTrue(result.requires_confirmation)
        self.assert_assigned(self.plan(override=True), spare)

    def test_uses_remaining_parallel_capacity(self):
        machine = self.machine.target
        machine.parallel_capacity = 2
        machine.save(update_fields=["parallel_capacity"])
        self.book_machine(self.machine)

        self.assert_assigned(self.plan(), self.machine)

    def test_unattended_machine_keeps_shared_running_time_capacity(self):
        machine = self.machine.target
        machine.needs_human = False
        machine.parallel_capacity = 2
        machine.daily_capacity_minutes = 60
        machine.save(update_fields=["needs_human", "parallel_capacity", "daily_capacity_minutes"])
        self.book_machine(self.machine)

        self.assert_assigned(self.plan(), self.machine)

    def test_pool_excludes_foreign_inactive_and_incompatible_machines(self):
        machine = self.machine.target
        machine.is_active = False
        machine.save(update_fields=["is_active"])
        resource_for(make_machine(make_company(name="Other"), name="Foreign"))
        resource_for(make_machine(self.company, name="Inactive", is_active=False))
        resource_for(make_machine(self.company, name="Painter", stages=[STAGE_PAINT]))
        disabled = resource_for(make_machine(self.company, name="Disabled resource"))
        disabled.is_active = False
        disabled.save(update_fields=["is_active"])

        result = self.plan(override=True)

        self.assertTrue(result.ok, result.conflicts)
        self.assertEqual(
            list(result.sessions[0].resources.values_list("resource_id", flat=True)),
            [self.operator.pk],
        )

    def test_foreign_operator_is_rejected(self):
        foreign = resource_for(make_employee(make_company(name="Other")))

        result = self.plan(operator=foreign, override=True)

        self.assertTrue(result.blocked)
        self.assertFalse(PlannedProductionSession.objects.filter(production_ticket=self.ticket).exists())

    def test_operator_must_be_qualified_for_the_stage(self):
        painter = resource_for(make_employee(self.company, stages=[STAGE_PAINT]))

        result = self.plan(operator=painter, override=True)

        self.assertTrue(result.blocked)
        self.assertFalse(PlannedProductionSession.objects.filter(production_ticket=self.ticket).exists())

    def test_multiple_required_operators_cannot_be_silently_reduced(self):
        ticket = make_ticket(
            self.company, stages=[STAGE_CUT], stage_minutes={STAGE_CUT: 60},
            machine_qty=1, employee_qty=2,
        )

        result = self.plan(ticket=ticket, override=True)

        self.assertTrue(result.blocked)
        self.assertFalse(PlannedProductionSession.objects.filter(production_ticket=ticket).exists())
