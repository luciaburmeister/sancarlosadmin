from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import patch

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from configurator.models import (
    Configuration,
    ConfigurationParameterValue,
    ProductParameter,
)
from management.models import ProjectFile
from production.models import ProductionItem, ProductionItemStatus
from production.services.planning_backlog import (
    URGENCY_LATER,
    URGENCY_NONE,
    URGENCY_OVERDUE,
    URGENCY_SOON,
    _candidate_tickets,
    unplanned_tickets,
)
from production.services.planning_stages import resolve_ticket_stages
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
from sales.models import Client, Quote


class PlanningBacklogTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = make_company()
        add_schedule(cls.company)
        cls.tz = timezone.get_current_timezone()
        cls.operator = resource_for(make_employee(cls.company))
        cls.today = timezone.localdate()

    def ticket_with_delivery(self, delivery_date, **kwargs):
        ticket = make_ticket(self.company, **kwargs)
        configuration = Configuration.objects.get(pk=ticket.object_id)
        client = Client.objects.create(company=self.company, name="Acme")
        configuration.quote = Quote.objects.create(
            company=self.company,
            client=client,
            estimated_delivery_date=delivery_date,
        )
        configuration.save(update_fields=["quote"])
        return ticket

    def ids(self, items):
        return [item.ticket_id for item in items]

    # --- membership -------------------------------------------------------

    def test_an_unplanned_ticket_appears(self):
        ticket = make_ticket(self.company)

        self.assertEqual(self.ids(unplanned_tickets(self.company)), [ticket.pk])

    def test_unplanning_returns_the_ticket_to_the_panel(self):
        ticket = make_ticket(self.company)
        sessions = [
            make_session(
                ticket,
                stage=stage,
                sequence=sequence,
                start=datetime(2026, 9, 14, 9 + sequence, tzinfo=self.tz),
                end=datetime(2026, 9, 14, 10 + sequence, tzinfo=self.tz),
                resources=[self.operator],
            )
            for sequence, stage in enumerate((STAGE_CUT, STAGE_WELD, STAGE_PAINT))
        ]
        self.assertEqual(unplanned_tickets(self.company), [])

        sessions[0].delete()

        self.assertEqual(self.ids(unplanned_tickets(self.company)), [ticket.pk])

    def test_a_partly_planned_ticket_keeps_its_remaining_stages(self):
        ticket = make_ticket(self.company)
        make_session(
            ticket,
            stage=STAGE_CUT,
            start=datetime(2026, 9, 14, 9, tzinfo=self.tz),
            end=datetime(2026, 9, 14, 10, tzinfo=self.tz),
            resources=[self.operator],
        )

        item = unplanned_tickets(self.company)[0]

        self.assertEqual(item.ticket_id, ticket.pk)
        self.assertEqual(item.total_minutes, 0)
        self.assertEqual(
            [(stage["stage"], stage["is_planned"]) for stage in item.stages],
            [(STAGE_CUT, True), (STAGE_WELD, False), (STAGE_PAINT, False)],
        )

    def test_completed_stages_are_not_pending_even_without_calendar_sessions(self):
        ticket = make_ticket(self.company)
        ProductionItem.objects.create(
            ticket=ticket,
            company=self.company,
            planning={
                STAGE_CUT: {"status": ProductionItemStatus.DONE},
                STAGE_WELD: {"status": ProductionItemStatus.IN_PROGRESS},
                STAGE_PAINT: {"status": ProductionItemStatus.PENDING},
            },
        )

        item = unplanned_tickets(self.company)[0]

        self.assertEqual([stage["stage"] for stage in item.stages], [STAGE_WELD, STAGE_PAINT])
        self.assertEqual([stage["position"] for stage in item.stages], [2, 3])
        self.assertEqual(item.current_stage, STAGE_WELD)
        self.assertEqual(item.stage_count, 2)
        self.assertEqual(item.completed_stage_count, 1)
        self.assertTrue(item.stages[0]["is_in_progress"])
        self.assertEqual(item.as_dict()["completed_stage_count"], 1)

    def test_a_stage_done_for_only_one_production_item_remains_pending(self):
        ticket = make_ticket(self.company)
        for status in (ProductionItemStatus.DONE, ProductionItemStatus.PENDING):
            ProductionItem.objects.create(
                ticket=ticket,
                company=self.company,
                planning={STAGE_CUT: {"status": status}},
            )

        item = unplanned_tickets(self.company)[0]

        self.assertEqual(item.stage_count, 3)
        self.assertEqual(item.completed_stage_count, 0)
        self.assertEqual(item.current_stage, STAGE_CUT)
        self.assertTrue(item.stages[0]["is_in_progress"])

    def test_a_scheduled_unfinished_stage_is_visible_but_not_actionable(self):
        ticket = make_ticket(self.company)
        ProductionItem.objects.create(
            ticket=ticket,
            company=self.company,
            planning={STAGE_CUT: {"status": ProductionItemStatus.IN_PROGRESS}},
        )
        make_session(
            ticket,
            stage=STAGE_CUT,
            start=datetime(2026, 9, 14, 9, tzinfo=self.tz),
            end=datetime(2026, 9, 14, 10, tzinfo=self.tz),
            resources=[self.operator],
        )

        item = unplanned_tickets(self.company)[0]

        self.assertEqual(item.current_stage, STAGE_CUT)
        self.assertTrue(item.stages[0]["is_planned"])
        self.assertEqual(item.as_dict()["planned_stage_count"], 1)

    def test_other_companies_are_not_visible(self):
        other = make_company(name="Otra SL")
        make_ticket(other)

        self.assertEqual(unplanned_tickets(self.company), [])

    def test_a_cancelled_ticket_is_excluded(self):
        ticket = make_ticket(self.company)
        ticket.status = "cancelled"
        ticket.save(update_fields=["status"])

        self.assertEqual(unplanned_tickets(self.company), [])

    def test_an_empty_backlog_short_circuits(self):
        self.assertEqual(unplanned_tickets(self.company), [])

    # --- ordering ---------------------------------------------------------

    def test_nearest_delivery_first_and_undated_last(self):
        later = self.ticket_with_delivery(self.today + timedelta(days=30))
        undated = make_ticket(self.company)
        soonest = self.ticket_with_delivery(self.today + timedelta(days=2))

        items = unplanned_tickets(self.company)

        self.assertEqual(self.ids(items), [soonest.pk, later.pk, undated.pk])

    def test_undated_tickets_tie_break_by_age(self):
        first = make_ticket(self.company)
        second = make_ticket(self.company)

        self.assertEqual(
            self.ids(unplanned_tickets(self.company)), [first.pk, second.pk]
        )

    def test_limit_is_applied_after_sorting(self):
        self.ticket_with_delivery(self.today + timedelta(days=30))
        soonest = self.ticket_with_delivery(self.today + timedelta(days=1))

        items = unplanned_tickets(self.company, limit=1)

        self.assertEqual(self.ids(items), [soonest.pk])

    def test_batching_skips_ineligible_tickets_before_applying_limit(self):
        self.ticket_with_delivery(self.today, stages=[])
        planned = self.ticket_with_delivery(
            self.today + timedelta(days=1), stages=[STAGE_CUT]
        )
        make_session(
            planned,
            stage=STAGE_CUT,
            start=datetime(2026, 9, 14, 9, tzinfo=self.tz),
            end=datetime(2026, 9, 14, 10, tzinfo=self.tz),
            resources=[self.operator],
        )
        first = self.ticket_with_delivery(self.today + timedelta(days=2))
        second = self.ticket_with_delivery(self.today + timedelta(days=3))
        self.ticket_with_delivery(self.today + timedelta(days=4))

        with patch("production.services.planning_backlog.BATCH_SIZE", 2), patch(
            "production.services.planning_backlog._candidate_tickets",
            wraps=_candidate_tickets,
        ) as candidates:
            items = unplanned_tickets(self.company, limit=2)

        self.assertEqual(self.ids(items), [first.pk, second.pk])
        self.assertEqual(candidates.call_count, 2)

    def test_batching_keeps_undated_tickets_last_and_ties_in_ticket_order(self):
        undated = make_ticket(self.company)
        later = self.ticket_with_delivery(self.today + timedelta(days=3))
        first = self.ticket_with_delivery(self.today + timedelta(days=1))
        second = self.ticket_with_delivery(self.today + timedelta(days=1))

        with patch("production.services.planning_backlog.BATCH_SIZE", 2):
            items = unplanned_tickets(self.company, limit=4)

        self.assertEqual(self.ids(items), [first.pk, second.pk, later.pk, undated.pk])

    def test_zero_limit_does_not_fetch_tickets(self):
        with patch("production.services.planning_backlog._candidate_tickets") as candidates:
            self.assertEqual(unplanned_tickets(self.company, limit=0), [])

        candidates.assert_not_called()

    # --- presentation -----------------------------------------------------

    def test_urgency_reflects_the_delivery_date(self):
        overdue = self.ticket_with_delivery(self.today - timedelta(days=1))
        soon = self.ticket_with_delivery(self.today + timedelta(days=3))
        later = self.ticket_with_delivery(self.today + timedelta(days=60))
        undated = make_ticket(self.company)

        urgency = {item.ticket_id: item.urgency for item in unplanned_tickets(self.company)}

        self.assertEqual(urgency[overdue.pk], URGENCY_OVERDUE)
        self.assertEqual(urgency[soon.pk], URGENCY_SOON)
        self.assertEqual(urgency[later.pk], URGENCY_LATER)
        self.assertEqual(urgency[undated.pk], URGENCY_NONE)

    def test_total_minutes_come_from_the_configured_stages(self):
        make_ticket(
            self.company,
            stages=[STAGE_CUT, STAGE_WELD],
            stage_minutes={STAGE_CUT: 45, STAGE_WELD: 90},
            employee_qty=1,
        )

        item = unplanned_tickets(self.company)[0]

        self.assertEqual(item.total_minutes, 135)
        self.assertEqual(item.stage_count, 2)
        self.assertEqual(item.hours_label, "2h 15m")
        self.assertFalse(item.needs_time_configuration)

    def test_a_ticket_without_durations_can_be_configured_in_the_calendar(self):
        make_ticket(self.company)

        item = unplanned_tickets(self.company)[0]

        self.assertEqual(item.total_minutes, 0)
        self.assertEqual(item.stage_count, 3)
        self.assertTrue(item.needs_time_configuration)
        self.assertEqual(
            [stage["stage"] for stage in item.stages],
            [STAGE_CUT, STAGE_WELD, "pintura"],
        )

    def test_stage_payload_lists_only_resources_compatible_with_its_stage(self):
        ticket = make_ticket(self.company, stages=[STAGE_CUT])
        cutter = resource_for(make_machine(self.company, name="Corte", stages=[STAGE_CUT]))
        painter = resource_for(make_machine(self.company, name="Pintura", stages=[STAGE_PAINT]))
        unconfigured = resource_for(make_machine(self.company, name="Sin etapa", stages=[]))

        stage = unplanned_tickets(self.company)[0].stages[0]

        self.assertEqual(stage["stage"], STAGE_CUT)
        self.assertIn(self.operator.pk, stage["compatible_resource_ids"])
        self.assertIn(cutter.pk, stage["compatible_resource_ids"])
        self.assertNotIn(painter.pk, stage["compatible_resource_ids"])
        self.assertNotIn(unconfigured.pk, stage["compatible_resource_ids"])

    def test_a_ticket_without_product_stages_is_not_shown(self):
        make_ticket(self.company, stages=[])

        self.assertEqual(unplanned_tickets(self.company), [])

    def test_a_ticket_with_a_deleted_source_is_not_shown(self):
        ticket = make_ticket(self.company)
        ticket.object_id += 1_000_000
        ticket.save(update_fields=["object_id"])
        ticket.refresh_from_db()

        self.assertEqual(unplanned_tickets(self.company), [])

    def test_a_partially_configured_ticket_keeps_defaults_and_requests_the_rest(self):
        make_ticket(
            self.company,
            stages=[STAGE_CUT, STAGE_WELD],
            stage_minutes={STAGE_CUT: 45},
        )

        item = unplanned_tickets(self.company)[0]

        self.assertEqual(
            [stage["total_time_min"] for stage in item.stages], [45, 0]
        )
        self.assertTrue(item.needs_time_configuration)

    def test_card_uses_ticket_and_configuration_instead_of_expediente(self):
        ticket = make_ticket(self.company)
        configuration = ticket.source_object
        configuration.name = "Window A"
        configuration.save(update_fields=["name"])
        second_ticket = make_ticket(self.company)
        second_configuration = second_ticket.source_object
        second_configuration.name = "Window B"
        second_configuration.save(update_fields=["name"])
        project_file = ProjectFile.objects.create(
            company=self.company,
            client=Client.objects.create(company=self.company, name="Acme"),
            name="Shared expediente",
            reference="EXP-123",
        )
        for current_ticket in (ticket, second_ticket):
            current_ticket.project_file = project_file
            current_ticket.save(update_fields=["project_file"])

        first, second = unplanned_tickets(self.company)

        self.assertEqual(first.label, f"OP-{ticket.pk}")
        self.assertEqual(first.configuration_label, f"#{configuration.pk} · Window A")
        self.assertEqual(second.label, f"OP-{second_ticket.pk}")
        self.assertEqual(
            second.configuration_label, f"#{second_configuration.pk} · Window B"
        )
        self.assertNotEqual(first.label, second.label)
        self.assertNotIn("EXP-123", first.label)

    # --- cost -------------------------------------------------------------

    def test_the_membership_query_is_flat_regardless_of_backlog_size(self):
        """Ticket, pricing and stage data load in a fixed number of queries."""
        for _ in range(12):
            self.ticket_with_delivery(self.today + timedelta(days=5))

        with self.assertNumQueries(8):
            candidates = _candidate_tickets(self.company)

        self.assertEqual(len(candidates), 12)

    def test_query_cost_per_ticket_stays_linear_not_quadratic(self):
        """A larger backlog must not add per-ticket queries to the board."""
        for _ in range(4):
            self.ticket_with_delivery(self.today + timedelta(days=5))

        with CaptureQueriesContext(connection) as small:
            unplanned_tickets(self.company)

        for _ in range(4):
            self.ticket_with_delivery(self.today + timedelta(days=5))

        with CaptureQueriesContext(connection) as large:
            unplanned_tickets(self.company)

        small_n, large_n = len(small.captured_queries), len(large.captured_queries)
        self.assertLessEqual(large_n, small_n + 1)

    def test_configured_durations_do_not_add_queries_per_ticket(self):
        for _ in range(4):
            self.ticket_with_delivery(
                self.today + timedelta(days=5), stage_minutes={STAGE_CUT: 30}
            )

        with CaptureQueriesContext(connection) as small:
            unplanned_tickets(self.company)

        for _ in range(4):
            self.ticket_with_delivery(
                self.today + timedelta(days=5), stage_minutes={STAGE_CUT: 30}
            )

        with CaptureQueriesContext(connection) as large:
            unplanned_tickets(self.company)

        self.assertLessEqual(len(large), len(small) + 1)

    def test_prefetched_parameter_values_still_resolve_stage_duration(self):
        ticket = make_ticket(
            self.company, stages=[STAGE_CUT], stage_minutes={STAGE_CUT: 30}
        )
        configuration = ticket.source_object
        parameter = ProductParameter.objects.create(
            product=configuration.product,
            name="minutes",
            label="Minutes",
        )
        ConfigurationParameterValue.objects.create(
            configuration=configuration, parameter=parameter, value="45"
        )
        item = configuration.items.get()
        item.stage_resources[STAGE_CUT]["work_time_min"] = "minutes"
        item.save(update_fields=["stage_resources"])

        self.assertEqual(unplanned_tickets(self.company)[0].total_minutes, 45)


class TemplateDurationFallbackTest(TestCase):
    """A stage's time comes from the product template when the item's snapshot
    predates it -- the board would otherwise ask for a duration the template
    already defines."""

    @classmethod
    def setUpTestData(cls):
        cls.company = make_company()
        add_schedule(cls.company)

    def stale_snapshot_ticket(self, **kwargs):
        """A ticket whose BOM was built before the template had any times:
        the item lists its stages but carries no stage_resources for them."""
        ticket = make_ticket(
            self.company,
            stages=[STAGE_CUT, STAGE_WELD],
            stage_minutes={STAGE_CUT: 30, STAGE_WELD: 20},
            **kwargs,
        )
        configuration = Configuration.objects.get(pk=ticket.object_id)
        item = configuration.items.get(is_template_item=True)
        item.stage_resources = {}
        item.save(update_fields=["stage_resources"])
        return ticket, configuration

    def test_empty_snapshot_falls_back_to_the_template(self):
        ticket, configuration = self.stale_snapshot_ticket()
        product = configuration.product
        product.production_planning_resources = {
            STAGE_CUT: {"prep_time_min": 5, "work_time_min": 45, "post_time_min": 10},
        }
        product.production_planning_configured = True
        product.save(update_fields=[
            "production_planning_resources", "production_planning_configured",
        ])

        stages = {
            s["stage"]: s["total_time_min"]
            for s in resolve_ticket_stages(ticket, include_unconfigured=True)
        }

        self.assertEqual(stages[STAGE_CUT], 60)
        # The template says nothing about welding, so it still needs a duration.
        self.assertEqual(stages[STAGE_WELD], 0)

    def test_a_populated_snapshot_still_wins_over_the_template(self):
        """A per-order time entered by a planner is not overwritten by the template."""
        ticket = make_ticket(
            self.company, stages=[STAGE_CUT], stage_minutes={STAGE_CUT: 30}
        )
        product = Configuration.objects.get(pk=ticket.object_id).product
        product.production_planning_resources = {
            STAGE_CUT: {"prep_time_min": 0, "work_time_min": 999, "post_time_min": 0},
        }
        product.save(update_fields=["production_planning_resources"])

        stages = {
            s["stage"]: s["total_time_min"]
            for s in resolve_ticket_stages(ticket, include_unconfigured=True)
        }

        self.assertEqual(stages[STAGE_CUT], 30)

    def test_an_unconfigured_template_still_needs_a_manual_duration(self):
        """Most templates have no times at all; those must keep asking."""
        ticket, _configuration = self.stale_snapshot_ticket()

        stages = resolve_ticket_stages(ticket, include_unconfigured=True)

        self.assertTrue(stages)
        self.assertTrue(all(s["total_time_min"] == 0 for s in stages))
