from __future__ import annotations

from datetime import datetime, timedelta

from django.db import IntegrityError, connection, transaction
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from configurator.models import Configuration, ConfigurationPlanningStatus
from production.models import (
    PlannedProductionSession,
    PlannedSessionResource,
    ProductionItem,
    ProductionResource,
)
from production.services.planning_conflicts import ConflictCode
from production.services.planning_stages import resolve_ticket_stages
from production.services.production_planning import ProductionPlanningService
from production.tests.factories import (
    STAGE_CUT,
    STAGE_PAINT,
    STAGE_WELD,
    add_schedule,
    make_company,
    make_employee,
    make_machine,
    make_session,
    make_ticket,
    resource_for,
)

MONDAY_0800 = datetime(2026, 9, 14, 8, 0)


class PlanTicketTest(TestCase):
    """A stage needing a machine AND an operator must produce ONE session
    carrying both -- the defect this whole feature is built on."""

    @classmethod
    def setUpTestData(cls):
        cls.company = make_company()
        add_schedule(cls.company)
        cls.tz = timezone.get_current_timezone()
        cls.start = MONDAY_0800.replace(tzinfo=cls.tz)

        cls.operator = resource_for(make_employee(cls.company, first_name="Ana"))
        cls.cnc = resource_for(make_machine(cls.company, name="CNC-01"))

        cls.ticket = make_ticket(
            cls.company,
            stage_minutes={STAGE_CUT: 60, STAGE_WELD: 120},
            machine_qty=1,
            employee_qty=1,
        )

    def test_one_session_per_stage_with_every_resource_attached(self):
        result = ProductionPlanningService.plan_ticket(
            self.ticket.pk, start_at=self.start
        )

        self.assertTrue(result.ok, result.conflicts)
        self.assertEqual(PlannedProductionSession.objects.count(), 2)
        # 2 stages x (1 machine + 1 operator)
        self.assertEqual(PlannedSessionResource.objects.count(), 4)

        for session in PlannedProductionSession.objects.all():
            self.assertEqual(
                set(session.resources.values_list("resource_id", flat=True)),
                {self.cnc.pk, self.operator.pk},
            )

    def test_anchor_is_the_machine(self):
        ProductionPlanningService.plan_ticket(self.ticket.pk, start_at=self.start)

        for session in PlannedProductionSession.objects.all():
            self.assertEqual(session.primary_resource_id, self.cnc.pk)

    def test_stages_are_chained_and_numbered(self):
        ProductionPlanningService.plan_ticket(self.ticket.pk, start_at=self.start)

        sessions = list(PlannedProductionSession.objects.order_by("sequence"))
        self.assertEqual([s.sequence for s in sessions], [0, 1])
        self.assertEqual([s.production_stage for s in sessions], [STAGE_CUT, STAGE_WELD])
        self.assertEqual(sessions[0].planned_start, self.start)
        self.assertEqual(sessions[0].planned_end, sessions[1].planned_start)
        self.assertEqual(sessions[0].duration_minutes, 60)
        self.assertEqual(sessions[1].duration_minutes, 120)

    def test_work_is_pushed_past_the_lunch_break(self):
        """A 120-minute stage starting at 13:00 must finish at 16:00, not 15:00."""
        ticket = make_ticket(
            self.company, stage_minutes={STAGE_CUT: 120}, employee_qty=1
        )

        ProductionPlanningService.plan_ticket(
            ticket.pk, start_at=self.start.replace(hour=13)
        )

        session = PlannedProductionSession.objects.get(production_ticket=ticket)
        self.assertEqual(timezone.localtime(session.planned_end, self.tz).hour, 16)

    def test_planning_starts_at_the_next_open_moment(self):
        ticket = make_ticket(self.company, stage_minutes={STAGE_CUT: 60}, employee_qty=1)
        saturday = datetime(2026, 9, 12, 9, 0, tzinfo=self.tz)

        ProductionPlanningService.plan_ticket(ticket.pk, start_at=saturday)

        session = PlannedProductionSession.objects.get(production_ticket=ticket)
        local = timezone.localtime(session.planned_start, self.tz)
        self.assertEqual((local.weekday(), local.hour), (0, 8))  # Monday 08:00

    def test_anchor_resource_is_preferred_for_the_first_stage_it_can_do(self):
        spare = resource_for(make_machine(self.company, name="CNC-02"))

        ProductionPlanningService.plan_ticket(
            self.ticket.pk, start_at=self.start, anchor_resource_id=spare.pk
        )

        first = PlannedProductionSession.objects.order_by("sequence").first()
        self.assertIn(
            spare.pk, set(first.resources.values_list("resource_id", flat=True))
        )

    def test_nested_calendar_drop_prefers_machine_and_operator(self):
        spare = resource_for(make_machine(self.company, name="CNC-02"))
        mate = resource_for(make_employee(self.company, first_name="Luis"))

        ProductionPlanningService.plan_ticket(
            self.ticket.pk,
            start_at=self.start,
            preferred_resource_ids=[spare.pk, mate.pk],
        )

        first = PlannedProductionSession.objects.order_by("sequence").first()
        self.assertEqual(
            set(first.resources.values_list("resource_id", flat=True)),
            {spare.pk, mate.pk},
        )

    def test_ticket_without_configured_durations_is_refused(self):
        bare = make_ticket(self.company)  # no stage_resources snapshot

        result = ProductionPlanningService.plan_ticket(bare.pk, start_at=self.start)

        self.assertFalse(result.ok)
        self.assertTrue(result.blocked)
        self.assertEqual(PlannedProductionSession.objects.count(), 0)

    def test_planning_marks_the_configuration_as_planned(self):
        ProductionPlanningService.plan_ticket(self.ticket.pk, start_at=self.start)

        configuration = Configuration.objects.get(pk=self.ticket.object_id)
        self.assertEqual(
            configuration.planning_status, ConfigurationPlanningStatus.PLANNED
        )

    def test_same_ticket_stage_cannot_be_planned_twice_in_the_same_slot(self):
        ProductionPlanningService.plan_ticket(self.ticket.pk, start_at=self.start)

        result = ProductionPlanningService.plan_ticket(
            self.ticket.pk, start_at=self.start, override=True
        )

        self.assertFalse(result.ok)
        self.assertTrue(result.blocked)
        self.assertIn(
            ConflictCode.DUPLICATE_STAGE_SLOT,
            {conflict.code for conflict in result.conflicts},
        )
        self.assertEqual(
            PlannedProductionSession.objects.filter(
                production_ticket=self.ticket
            ).count(),
            2,
        )

    def test_database_rejects_duplicate_stage_time_slots_from_any_write_path(self):
        first = make_session(
            self.ticket,
            stage=STAGE_CUT,
            start=self.start,
            end=self.start + timedelta(hours=1),
            resources=[self.operator],
        )

        with self.assertRaises(IntegrityError), transaction.atomic():
            PlannedProductionSession.objects.create(
                production_ticket=self.ticket,
                company=self.company,
                production_stage=first.production_stage,
                planned_start=first.planned_start,
                planned_end=first.planned_end,
            )


class CustomPlanTicketTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = make_company()
        add_schedule(cls.company)
        cls.tz = timezone.get_current_timezone()
        cls.start = MONDAY_0800.replace(tzinfo=cls.tz)
        cls.operator = resource_for(make_employee(cls.company, first_name="Ana"))
        cls.cnc = resource_for(make_machine(cls.company, name="CNC-01"))

    def test_unconfigured_ticket_is_planned_with_entered_times_and_resources(self):
        ticket = make_ticket(
            self.company, stages=[STAGE_CUT, STAGE_WELD]
        )

        result = ProductionPlanningService.plan_ticket_custom(
            ticket.pk,
            start_at=self.start,
            stages=[
                {
                    "stage": STAGE_CUT,
                    "duration_minutes": 45,
                    "resource_ids": [self.cnc.pk, self.operator.pk],
                },
                {
                    "stage": STAGE_WELD,
                    "duration_minutes": 75,
                    "resource_ids": [self.operator.pk],
                },
            ],
        )

        self.assertTrue(result.ok, result.conflicts)
        sessions = list(
            PlannedProductionSession.objects.filter(
                production_ticket=ticket
            ).order_by("sequence")
        )
        self.assertEqual([session.production_stage for session in sessions], [STAGE_CUT, STAGE_WELD])
        self.assertEqual([session.duration_minutes for session in sessions], [45, 75])
        self.assertEqual(sessions[0].planned_end, sessions[1].planned_start)
        self.assertEqual(
            set(sessions[0].resources.values_list("resource_id", flat=True)),
            {self.cnc.pk, self.operator.pk},
        )

    def test_submitted_order_becomes_the_session_order(self):
        ticket = make_ticket(
            self.company, stages=[STAGE_CUT, STAGE_WELD]
        )

        ProductionPlanningService.plan_ticket_custom(
            ticket.pk,
            start_at=self.start,
            stages=[
                {
                    "stage": STAGE_WELD,
                    "duration_minutes": 30,
                    "resource_ids": [self.operator.pk],
                },
                {
                    "stage": STAGE_CUT,
                    "duration_minutes": 60,
                    "resource_ids": [self.operator.pk],
                },
            ],
        )

        self.assertEqual(
            list(
                PlannedProductionSession.objects.filter(production_ticket=ticket)
                .order_by("sequence")
                .values_list("production_stage", flat=True)
            ),
            [STAGE_WELD, STAGE_CUT],
        )

    def test_a_single_stage_can_be_planned_without_the_rest_of_the_ticket(self):
        ticket = make_ticket(
            self.company, stages=[STAGE_CUT, STAGE_WELD]
        )

        result = ProductionPlanningService.plan_ticket_stage_custom(
            ticket.pk,
            start_at=self.start,
            stage={
                "stage": STAGE_CUT,
                "duration_minutes": 45,
                "resource_ids": [self.cnc.pk, self.operator.pk],
            },
        )

        self.assertTrue(result.ok, result.conflicts)
        session = PlannedProductionSession.objects.get(production_ticket=ticket)
        self.assertEqual(session.production_stage, STAGE_CUT)
        self.assertEqual(session.sequence, 0)
        self.assertEqual(session.duration_minutes, 45)
        self.assertEqual(
            set(session.resources.values_list("resource_id", flat=True)),
            {self.cnc.pk, self.operator.pk},
        )

    def test_individual_stage_rejects_an_incompatible_resource_without_override(self):
        ticket = make_ticket(self.company, stages=[STAGE_CUT])
        painter = resource_for(
            make_employee(self.company, first_name="Paz", stages=[STAGE_PAINT])
        )

        result = ProductionPlanningService.plan_ticket_stage_custom(
            ticket.pk,
            start_at=self.start,
            stage={
                "stage": STAGE_CUT,
                "duration_minutes": 30,
                "resource_ids": [painter.pk],
            },
            override=True,
        )

        self.assertFalse(result.ok)
        self.assertTrue(result.blocked)
        self.assertFalse(result.requires_confirmation)
        self.assertIn(ConflictCode.RESOURCE_NOT_SKILLED, {c.code for c in result.conflicts})

    def test_individual_stage_requires_an_explicit_stage_assignment(self):
        ticket = make_ticket(self.company, stages=[STAGE_CUT])
        unconfigured = resource_for(
            make_employee(self.company, first_name="Eva", stages=[])
        )

        result = ProductionPlanningService.plan_ticket_stage_custom(
            ticket.pk,
            start_at=self.start,
            stage={
                "stage": STAGE_CUT,
                "duration_minutes": 30,
                "resource_ids": [unconfigured.pk],
            },
            override=True,
        )

        self.assertFalse(result.ok)
        self.assertTrue(result.blocked)
        self.assertIn(ConflictCode.RESOURCE_NOT_SKILLED, {c.code for c in result.conflicts})

    def test_individual_stage_requires_every_configured_resource_kind(self):
        ticket = make_ticket(
            self.company,
            stages=[STAGE_CUT],
            stage_minutes={STAGE_CUT: 30},
            machine_qty=1,
            employee_qty=1,
        )

        result = ProductionPlanningService.plan_ticket_stage_custom(
            ticket.pk,
            start_at=self.start,
            stage={
                "stage": STAGE_CUT,
                "duration_minutes": 30,
                "resource_ids": [self.operator.pk],
            },
            override=True,
        )

        self.assertFalse(result.ok)
        self.assertTrue(result.blocked)
        self.assertFalse(result.requires_confirmation)
        self.assertIn(ConflictCode.REQUIREMENT_UNMET, {c.code for c in result.conflicts})

    def test_an_individual_stage_cannot_be_added_twice(self):
        ticket = make_ticket(self.company, stages=[STAGE_CUT])
        payload = {
            "stage": STAGE_CUT,
            "duration_minutes": 30,
            "resource_ids": [self.operator.pk],
        }
        ProductionPlanningService.plan_ticket_stage_custom(
            ticket.pk, start_at=self.start, stage=payload
        )

        result = ProductionPlanningService.plan_ticket_stage_custom(
            ticket.pk, start_at=self.start, stage=payload
        )

        self.assertFalse(result.ok)
        self.assertTrue(result.blocked)
        self.assertEqual(
            PlannedProductionSession.objects.filter(production_ticket=ticket).count(),
            1,
        )

    def test_invalid_stage_payload_is_atomic(self):
        ticket = make_ticket(
            self.company, stages=[STAGE_CUT, STAGE_WELD]
        )

        result = ProductionPlanningService.plan_ticket_custom(
            ticket.pk,
            start_at=self.start,
            stages=[
                {"stage": STAGE_CUT, "duration_minutes": 30, "resource_ids": []},
            ],
        )

        self.assertFalse(result.ok)
        self.assertTrue(result.blocked)
        self.assertFalse(
            PlannedProductionSession.objects.filter(production_ticket=ticket).exists()
        )

    def test_every_stage_needs_at_least_one_resource(self):
        ticket = make_ticket(self.company, stages=[STAGE_CUT])

        result = ProductionPlanningService.plan_ticket_custom(
            ticket.pk,
            start_at=self.start,
            stages=[
                {"stage": STAGE_CUT, "duration_minutes": 30, "resource_ids": []}
            ],
        )

        self.assertFalse(result.ok)
        self.assertTrue(result.blocked)
        self.assertFalse(
            PlannedProductionSession.objects.filter(production_ticket=ticket).exists()
        )

    def test_resource_from_another_company_is_blocked(self):
        ticket = make_ticket(self.company, stages=[STAGE_CUT])
        other_company = make_company(name="Other")
        foreign = resource_for(make_employee(other_company))

        result = ProductionPlanningService.plan_ticket_custom(
            ticket.pk,
            start_at=self.start,
            stages=[
                {
                    "stage": STAGE_CUT,
                    "duration_minutes": 30,
                    "resource_ids": [foreign.pk],
                }
            ],
        )

        self.assertFalse(result.ok)
        self.assertTrue(result.blocked)
        self.assertFalse(
            PlannedProductionSession.objects.filter(production_ticket=ticket).exists()
        )


class MutationTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = make_company()
        add_schedule(cls.company)
        cls.tz = timezone.get_current_timezone()
        cls.start = MONDAY_0800.replace(tzinfo=cls.tz)

        cls.operator = resource_for(make_employee(cls.company, first_name="Ana"))
        cls.mate = resource_for(
            make_employee(cls.company, first_name="Luis", last_name="Vega")
        )
        cls.cnc = resource_for(make_machine(cls.company, name="CNC-01"))
        cls.ticket = make_ticket(cls.company)

    def session(self, **kwargs):
        kwargs.setdefault("start", self.start.replace(hour=9))
        kwargs.setdefault("end", self.start.replace(hour=11))
        kwargs.setdefault("resources", [self.cnc, self.operator])
        return make_session(self.ticket, **kwargs)

    # --- move ------------------------------------------------------------

    def test_move_preserves_duration_and_carries_every_resource(self):
        session = self.session()

        result = ProductionPlanningService.move_session(
            session.pk, new_start=self.start.replace(hour=12)
        )

        self.assertTrue(result.ok, result.conflicts)
        session.refresh_from_db()
        self.assertEqual(timezone.localtime(session.planned_start, self.tz).hour, 12)
        self.assertEqual(session.duration_minutes, 120)
        self.assertEqual(
            set(session.resources.values_list("resource_id", flat=True)),
            {self.cnc.pk, self.operator.pk},
        )

    def test_move_marks_the_session_as_manual(self):
        session = self.session(source="auto")

        ProductionPlanningService.move_session(
            session.pk, new_start=self.start.replace(hour=12)
        )

        session.refresh_from_db()
        self.assertEqual(session.source, "manual")

    def test_moving_a_missing_session_is_reported_not_crashed(self):
        result = ProductionPlanningService.move_session(999999, new_start=self.start)

        self.assertFalse(result.ok)
        self.assertTrue(result.blocked)

    # --- resize ----------------------------------------------------------

    def test_resize_changes_only_the_end(self):
        session = self.session()

        result = ProductionPlanningService.resize_session(
            session.pk, new_end=self.start.replace(hour=13)
        )

        self.assertTrue(result.ok, result.conflicts)
        session.refresh_from_db()
        self.assertEqual(session.duration_minutes, 240)

    # --- resources -------------------------------------------------------

    def test_set_resources_recomputes_the_anchor_to_the_machine(self):
        session = self.session(resources=[self.operator])
        self.assertEqual(session.primary_resource_id, self.operator.pk)

        result = ProductionPlanningService.set_resources(
            session.pk, resource_ids=[self.operator.pk, self.cnc.pk]
        )

        self.assertTrue(result.ok, result.conflicts)
        session.refresh_from_db()
        self.assertEqual(session.primary_resource_id, self.cnc.pk)

    def test_swapping_one_resource_keeps_the_others(self):
        """Dragging the machine half onto another machine keeps the operator."""
        session = self.session()
        spare = resource_for(make_machine(self.company, name="CNC-02"))

        ProductionPlanningService.set_resources(
            session.pk, resource_ids=[spare.pk, self.operator.pk]
        )

        session.refresh_from_db()
        self.assertEqual(
            set(session.resources.values_list("resource_id", flat=True)),
            {spare.pk, self.operator.pk},
        )

    def test_removing_a_resource_deletes_its_row(self):
        session = self.session()

        ProductionPlanningService.set_resources(
            session.pk, resource_ids=[self.operator.pk]
        )

        session.refresh_from_db()
        self.assertEqual(
            list(session.resources.values_list("resource_id", flat=True)),
            [self.operator.pk],
        )
        self.assertEqual(session.primary_resource_id, self.operator.pk)

    def test_set_resources_refuses_an_id_that_does_not_exist(self):
        """Without this the unknown id reaches the database as an FK violation."""
        session = self.session()

        result = ProductionPlanningService.set_resources(
            session.pk, resource_ids=[999999], override=True
        )

        self.assertFalse(result.ok)
        self.assertTrue(result.blocked)
        session.refresh_from_db()
        self.assertEqual(
            set(session.resources.values_list("resource_id", flat=True)),
            {self.cnc.pk, self.operator.pk},
        )

    def test_set_resources_refuses_a_deactivated_resource(self):
        session = self.session()
        retired = resource_for(make_machine(self.company, name="CNC-OLD"))
        ProductionResource.objects.filter(pk=retired.pk).update(is_active=False)

        result = ProductionPlanningService.set_resources(
            session.pk, resource_ids=[retired.pk], override=True
        )

        self.assertFalse(result.ok)
        self.assertTrue(result.blocked)

    def test_move_refuses_a_resource_from_another_company(self):
        session = self.session()
        original_start = session.planned_start
        other = resource_for(make_machine(make_company(name="Otra SL"), name="CNC-X"))

        result = ProductionPlanningService.move_session(
            session.pk,
            new_start=self.start.replace(hour=12),
            resource_ids=[other.pk],
            override=True,
        )

        self.assertFalse(result.ok)
        self.assertTrue(result.blocked)
        session.refresh_from_db()
        self.assertEqual(session.planned_start, original_start)

    # --- override semantics ----------------------------------------------

    def test_a_warning_without_override_persists_nothing(self):
        blocker = self.session(resources=[self.operator])
        mover = make_session(
            self.ticket,
            stage=STAGE_PAINT,
            start=self.start.replace(hour=12),
            end=self.start.replace(hour=13),
            resources=[self.operator],
            sequence=1,
        )
        original_start = mover.planned_start

        result = ProductionPlanningService.move_session(
            mover.pk, new_start=blocker.planned_start
        )

        self.assertFalse(result.ok)
        self.assertTrue(result.requires_confirmation)
        self.assertIn(
            ConflictCode.RESOURCE_OVERLAP, {c.code for c in result.conflicts}
        )
        mover.refresh_from_db()
        self.assertEqual(mover.planned_start, original_start)

    def test_override_applies_the_move_and_still_reports_the_conflict(self):
        blocker = self.session(resources=[self.operator])
        mover = make_session(
            self.ticket,
            stage=STAGE_PAINT,
            start=self.start.replace(hour=12),
            end=self.start.replace(hour=13),
            resources=[self.operator],
            sequence=1,
        )

        result = ProductionPlanningService.move_session(
            mover.pk, new_start=blocker.planned_start, override=True
        )

        self.assertTrue(result.ok)
        self.assertTrue(result.conflicts)  # reported, not swallowed
        mover.refresh_from_db()
        self.assertEqual(mover.planned_start, blocker.planned_start)

    def test_locked_session_refuses_even_with_override(self):
        session = self.session(locked=True)
        original_start = session.planned_start

        result = ProductionPlanningService.move_session(
            session.pk, new_start=self.start.replace(hour=12), override=True
        )

        self.assertFalse(result.ok)
        self.assertTrue(result.blocked)
        session.refresh_from_db()
        self.assertEqual(session.planned_start, original_start)

    # --- unplan ----------------------------------------------------------

    def test_unplan_returns_the_configuration_to_the_backlog(self):
        session = self.session()
        configuration = Configuration.objects.get(pk=self.ticket.object_id)
        self.assertEqual(
            configuration.planning_status, ConfigurationPlanningStatus.PLANNED
        )

        result = ProductionPlanningService.unplan(session.pk)

        self.assertTrue(result.ok)
        configuration.refresh_from_db()
        self.assertEqual(
            configuration.planning_status, ConfigurationPlanningStatus.UNPLANNED
        )

    def test_unplan_refuses_a_locked_session(self):
        session = self.session(locked=True)

        result = ProductionPlanningService.unplan(session.pk)

        self.assertFalse(result.ok)
        self.assertTrue(PlannedProductionSession.objects.filter(pk=session.pk).exists())

    def test_unplan_ticket_removes_every_unlocked_session(self):
        self.session()
        make_session(
            self.ticket,
            stage=STAGE_PAINT,
            start=self.start.replace(hour=12),
            end=self.start.replace(hour=13),
            resources=[self.cnc],
            sequence=1,
            locked=True,
        )

        ProductionPlanningService.unplan_ticket(self.ticket.pk)

        self.assertEqual(PlannedProductionSession.objects.count(), 1)
        self.assertTrue(PlannedProductionSession.objects.get().locked)

    # --- reorder ---------------------------------------------------------

    def test_reorder_renumbers_and_reflows_the_stages(self):
        first = make_session(
            self.ticket,
            stage=STAGE_CUT,
            start=self.start.replace(hour=8),
            end=self.start.replace(hour=9),
            resources=[self.operator],
            sequence=0,
        )
        second = make_session(
            self.ticket,
            stage=STAGE_PAINT,
            start=self.start.replace(hour=9),
            end=self.start.replace(hour=11),
            resources=[self.cnc],
            sequence=1,
        )

        result = ProductionPlanningService.reorder(
            self.ticket.pk,
            session_id=second.pk,
            before_session_id=first.pk,
            override=True,
        )

        self.assertTrue(result.ok, result.conflicts)
        second.refresh_from_db()
        first.refresh_from_db()
        self.assertEqual((second.sequence, first.sequence), (0, 1))
        # Painting (2h) now runs first from 08:00, cutting (1h) follows it.
        self.assertEqual(timezone.localtime(second.planned_start, self.tz).hour, 8)
        self.assertEqual(second.planned_end, first.planned_start)
        self.assertEqual(first.duration_minutes, 60)

    def test_reorder_query_cost(self):
        from production.views.production_schedule_view import _session_json

        sessions = [
            make_session(
                self.ticket,
                stage=f"stage-{sequence}",
                start=self.start + timedelta(hours=sequence),
                end=self.start + timedelta(hours=sequence + 1),
                resources=[self.operator],
                sequence=sequence,
            )
            for sequence in range(4)
        ]

        with CaptureQueriesContext(connection) as queries:
            result = ProductionPlanningService.reorder(
                self.ticket.pk,
                session_id=sessions[-1].pk,
                before_session_id=sessions[0].pk,
                override=True,
            )
            serialized = [_session_json(session, None) for session in result.sessions]

        self.assertTrue(result.ok, result.conflicts)
        self.assertEqual(
            [item["resource_ids"] for item in serialized],
            [[self.operator.pk]] * 4,
        )
        self.assertLessEqual(len(queries), 32)

    def test_reorder_still_heals_planning_status(self):
        first = make_session(
            self.ticket,
            stage=STAGE_CUT,
            start=self.start,
            end=self.start + timedelta(hours=1),
            resources=[self.operator],
            sequence=0,
        )
        second = make_session(
            self.ticket,
            stage=STAGE_PAINT,
            start=self.start + timedelta(hours=1),
            end=self.start + timedelta(hours=2),
            resources=[self.operator],
            sequence=1,
        )
        Configuration.objects.filter(pk=self.ticket.object_id).update(
            planning_status=ConfigurationPlanningStatus.UNPLANNED
        )

        result = ProductionPlanningService.reorder(
            self.ticket.pk,
            session_id=second.pk,
            before_session_id=first.pk,
            override=True,
        )

        self.assertTrue(result.ok, result.conflicts)
        self.assertEqual(
            Configuration.objects.get(pk=self.ticket.object_id).planning_status,
            ConfigurationPlanningStatus.PLANNED,
        )

    def test_reorder_without_cascade_only_renumbers(self):
        first = make_session(
            self.ticket,
            stage=STAGE_CUT,
            start=self.start.replace(hour=8),
            end=self.start.replace(hour=9),
            resources=[self.operator],
            sequence=0,
        )
        second = make_session(
            self.ticket,
            stage=STAGE_PAINT,
            start=self.start.replace(hour=9),
            end=self.start.replace(hour=11),
            resources=[self.cnc],
            sequence=1,
        )
        original = second.planned_start

        ProductionPlanningService.reorder(
            self.ticket.pk,
            session_id=second.pk,
            before_session_id=first.pk,
            cascade=False,
            override=True,
        )

        second.refresh_from_db()
        self.assertEqual(second.sequence, 0)
        self.assertEqual(second.planned_start, original)

    def test_reorder_uses_the_complete_ticket_even_when_a_stage_is_on_another_day(self):
        first = make_session(
            self.ticket,
            stage=STAGE_CUT,
            start=self.start.replace(hour=8),
            end=self.start.replace(hour=9),
            resources=[self.operator],
            sequence=0,
        )
        second = make_session(
            self.ticket,
            stage=STAGE_PAINT,
            start=self.start.replace(hour=9),
            end=self.start.replace(hour=11),
            resources=[self.cnc],
            sequence=1,
        )
        later = make_session(
            self.ticket,
            stage=STAGE_WELD,
            start=(self.start + timedelta(days=1)).replace(hour=8),
            end=(self.start + timedelta(days=1)).replace(hour=9),
            resources=[self.operator],
            sequence=2,
        )

        result = ProductionPlanningService.reorder(
            self.ticket.pk,
            session_id=second.pk,
            before_session_id=first.pk,
        )

        self.assertTrue(result.ok, result.conflicts)
        sessions = list(
            PlannedProductionSession.objects.filter(production_ticket=self.ticket).order_by(
                "sequence"
            )
        )
        self.assertEqual([session.pk for session in sessions], [second.pk, first.pk, later.pk])
        self.assertEqual([session.sequence for session in sessions], [0, 1, 2])
        second.refresh_from_db()
        first.refresh_from_db()
        later.refresh_from_db()
        self.assertEqual(second.planned_end, first.planned_start)
        self.assertEqual(first.planned_end, later.planned_start)

    def test_reorder_persists_the_ticket_workflow_order_without_touching_the_template(self):
        first = make_session(
            self.ticket,
            stage=STAGE_CUT,
            start=self.start.replace(hour=8),
            end=self.start.replace(hour=9),
            resources=[self.operator],
            sequence=0,
        )
        second = make_session(
            self.ticket,
            stage=STAGE_PAINT,
            start=self.start.replace(hour=9),
            end=self.start.replace(hour=10),
            resources=[self.cnc],
            sequence=1,
        )
        item = ProductionItem.objects.create(
            ticket=self.ticket,
            company=self.company,
            production_stages_order=[STAGE_CUT, STAGE_PAINT],
            planning={STAGE_CUT: {}, STAGE_PAINT: {}},
        )
        product_order = list(self.ticket.source_object.product.production_stages)

        result = ProductionPlanningService.reorder(
            self.ticket.pk,
            session_id=second.pk,
            before_session_id=first.pk,
        )

        self.assertTrue(result.ok, result.conflicts)
        self.ticket.refresh_from_db()
        self.assertEqual(self.ticket.get_stage_order()[:2], [STAGE_PAINT, STAGE_CUT])
        self.assertEqual(
            [stage for stage, _info in item.get_ordered_planning()],
            [STAGE_PAINT, STAGE_CUT],
        )
        self.assertEqual(
            [stage["stage"] for stage in resolve_ticket_stages(self.ticket, include_unconfigured=True)][:2],
            [STAGE_PAINT, STAGE_CUT],
        )
        self.assertEqual(self.ticket.source_object.product.production_stages, product_order)

    def test_ordered_planning_appends_an_item_stage_the_ticket_order_does_not_know(self):
        """A ticket-level reorder should not silently drop a stage that only
        one item has (e.g. a component-specific extra step)."""
        self.ticket.stage_order = [STAGE_PAINT, STAGE_CUT]
        self.ticket.save(update_fields=["stage_order"])
        item = ProductionItem.objects.create(
            ticket=self.ticket,
            company=self.company,
            production_stages_order=[STAGE_CUT, STAGE_PAINT, STAGE_WELD],
            planning={STAGE_CUT: {}, STAGE_PAINT: {}, STAGE_WELD: {}},
        )

        self.assertEqual(
            [stage for stage, _info in item.get_ordered_planning()],
            [STAGE_PAINT, STAGE_CUT, STAGE_WELD],
        )

    def test_reorder_refuses_to_change_a_locked_stage(self):
        first = make_session(
            self.ticket,
            stage=STAGE_CUT,
            start=self.start.replace(hour=8),
            end=self.start.replace(hour=9),
            resources=[self.operator],
            sequence=0,
            locked=True,
        )
        second = make_session(
            self.ticket,
            stage=STAGE_PAINT,
            start=self.start.replace(hour=9),
            end=self.start.replace(hour=10),
            resources=[self.cnc],
            sequence=1,
        )

        result = ProductionPlanningService.reorder(
            self.ticket.pk,
            session_id=second.pk,
            before_session_id=first.pk,
            override=True,
        )

        self.assertFalse(result.ok)
        self.assertTrue(result.blocked)
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual((first.sequence, second.sequence), (0, 1))
