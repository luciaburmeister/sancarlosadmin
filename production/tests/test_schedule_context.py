from __future__ import annotations

from datetime import datetime, timedelta

from django.test import TestCase
from django.utils import timezone
from django.utils.translation import gettext as _

from management.models import ProjectFile
from production.models import ProductionItem, ProductionItemStatus
from production.services.schedule_context import (
    STATE_DONE,
    STATE_PLANNED,
    STATE_RUNNING,
    ProductionScheduleContext,
    stage_abbreviation,
    text_color_for,
    ticket_color,
)
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
from sales.models import Client

def _upcoming_monday() -> datetime:
    """The Monday of next week.

    This suite needs one anchor that is reliably *ahead* of now -- nothing
    planned on it is late, and the now-line falls outside its window -- while a
    fortnight earlier is reliably behind it. Pinning that to a literal date
    only holds until the date arrives, at which point those tests start failing
    for a day with nothing actually broken.
    """
    today = timezone.localdate()
    monday = today - timedelta(days=today.weekday()) + timedelta(days=7)
    return datetime(monday.year, monday.month, monday.day)


MONDAY = _upcoming_monday()


class ScheduleContextTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = make_company()
        add_schedule(cls.company)
        cls.tz = timezone.get_current_timezone()

        cls.ana = make_employee(cls.company, first_name="Ana", last_name="Ruiz")
        cls.cnc = make_machine(cls.company, name="CNC-01")
        cls.operator = resource_for(cls.ana)
        cls.machine = resource_for(cls.cnc)

        cls.ticket = make_ticket(cls.company)

    def at(self, hour: int, minute: int = 0) -> datetime:
        return MONDAY.replace(hour=hour, minute=minute, tzinfo=self.tz)

    def context(self, **kwargs):
        kwargs.setdefault("company", self.company)
        kwargs.setdefault("start", self.at(0))
        kwargs.setdefault("end", self.at(23, 59))
        return ProductionScheduleContext(**kwargs)

    def rows_by_label(self, data):
        return {row.label: row for row in data.machine_rows + data.employee_rows}

    def stage_group(self, data, stage):
        return next(group for group in data.stage_groups if group.stage == stage)

    # --- rows ------------------------------------------------------------

    def test_resources_are_grouped_into_machines_and_operators(self):
        data = self.context().build()

        self.assertEqual([row.label for row in data.machine_rows], ["CNC-01"])
        self.assertEqual([row.label for row in data.employee_rows], ["Ana Ruiz"])

    def test_inactive_resources_are_not_shown(self):
        self.machine.is_active = False
        self.machine.save(update_fields=["is_active"])

        data = self.context().build()

        self.assertNotIn("CNC-01", self.rows_by_label(data))

    def test_a_multi_resource_session_renders_in_every_resource_row(self):
        """One session, two rows, one shared identity -- the whole point."""
        session = make_session(
            self.ticket,
            start=self.at(9),
            end=self.at(11),
            resources=[self.machine, self.operator],
        )

        data = self.context().build()
        rows = self.rows_by_label(data)

        machine_blocks = rows["CNC-01"].blocks
        operator_blocks = rows["Ana Ruiz"].blocks
        self.assertEqual(len(machine_blocks), 1)
        self.assertEqual(len(operator_blocks), 1)
        self.assertEqual(machine_blocks[0].session_id, session.pk)
        self.assertEqual(operator_blocks[0].session_id, session.pk)
        # Same session, same time, same colour in both rows -- the identity the
        # browser keys on is data-session-id, which is how one drag updates
        # every copy. They are deliberately *not* the same object: how a block
        # is split across its row's height depends on what else shares that
        # row, which differs between the machine's row and the operator's.
        self.assertEqual(machine_blocks[0].left, operator_blocks[0].left)
        self.assertEqual(machine_blocks[0].width, operator_blocks[0].width)
        self.assertEqual(machine_blocks[0].color, operator_blocks[0].color)
        self.assertIsNot(machine_blocks[0], operator_blocks[0])

    def test_blocks_carry_every_resource_of_their_session(self):
        make_session(
            self.ticket,
            start=self.at(9),
            end=self.at(11),
            resources=[self.machine, self.operator],
        )

        data = self.context().build()
        block = self.rows_by_label(data)["CNC-01"].blocks[0]

        self.assertEqual(
            set(block.resource_ids), {self.machine.pk, self.operator.pk}
        )
        self.assertEqual(sorted(block.resource_labels), ["Ana Ruiz", "CNC-01"])

    def test_sessions_outside_the_window_are_excluded(self):
        make_session(
            self.ticket,
            start=self.at(9) + timedelta(days=7),
            end=self.at(11) + timedelta(days=7),
            resources=[self.operator],
        )

        data = self.context().build()

        self.assertEqual(self.rows_by_label(data)["Ana Ruiz"].blocks, [])

    def test_resource_filter_narrows_the_rows(self):
        data = self.context(resource_ids=[self.machine.pk]).build()

        self.assertEqual(list(self.rows_by_label(data)), ["CNC-01"])

    def test_machine_people_view_nests_assigned_operator(self):
        make_session(
            self.ticket,
            start=self.at(9),
            end=self.at(11),
            resources=[self.machine, self.operator],
        )

        group = self.stage_group(self.context().build(), STAGE_CUT).machine_groups[0]

        self.assertEqual(group.machine.label, "CNC-01")
        self.assertEqual([row.label for row in group.operator_rows], ["Ana Ruiz"])
        self.assertEqual(len(group.operator_rows[0].blocks), 1)

    def test_machine_people_view_lists_operators_with_a_shared_stage_before_assignment(self):
        cutter = resource_for(
            make_machine(self.company, name="Cutter", stages=[STAGE_CUT])
        )
        cut_operator = resource_for(
            make_employee(
                self.company,
                first_name="Carlos",
                last_name="Cut",
                stages=[STAGE_CUT],
            )
        )
        paint_operator = resource_for(
            make_employee(
                self.company,
                first_name="Paula",
                last_name="Paint",
                stages=[STAGE_PAINT],
            )
        )

        stage_group = self.stage_group(self.context().build(), STAGE_CUT)
        groups = {group.machine.resource_id: group for group in stage_group.machine_groups}
        operator_ids = {
            row.resource_id for row in groups[cutter.pk].operator_rows
        }

        self.assertIn(cut_operator.pk, operator_ids)
        self.assertNotIn(paint_operator.pk, operator_ids)

    def test_machine_only_work_is_explicitly_unassigned(self):
        make_session(
            self.ticket,
            start=self.at(9),
            end=self.at(11),
            resources=[self.machine],
        )

        group = self.stage_group(self.context().build(), STAGE_CUT).machine_groups[0]

        self.assertIsNotNone(group.unassigned_row)
        self.assertEqual(len(group.unassigned_row.blocks), 1)

    def test_operator_only_work_is_explicitly_unassigned_from_a_machine(self):
        session = make_session(
            self.ticket,
            start=self.at(9),
            end=self.at(11),
            resources=[self.operator],
        )

        group = self.stage_group(self.context().build(), STAGE_CUT)

        self.assertIsNotNone(group.unassigned_machine_row)
        self.assertFalse(group.has_single_machine)
        self.assertEqual(
            [block.session_id for block in group.unassigned_machine_row.blocks],
            [session.pk],
        )
        self.assertIn(session.pk, [block.session_id for block in group.blocks])

    def test_stages_without_machines_have_their_own_operator_sections(self):
        self.cnc.production_stages = [STAGE_CUT]
        self.cnc.save(update_fields=["production_stages"])

        stage_groups = [
            group
            for group in self.context().build().stage_groups
            if not group.machine_groups
        ]

        self.assertEqual([group.stage for group in stage_groups], [STAGE_WELD, STAGE_PAINT])
        self.assertEqual(
            [row.label for row in stage_groups[0].operator_rows], ["Ana Ruiz"]
        )

    def test_all_view_only_groups_company_stages(self):
        legacy = make_session(
            self.ticket,
            stage="legacy stage",
            start=self.at(9),
            end=self.at(11),
            resources=[self.machine, self.operator],
        )

        data = self.context().build()

        self.assertEqual(
            [group.stage for group in data.stage_groups],
            [STAGE_CUT, STAGE_WELD, STAGE_PAINT],
        )
        for rows in (data.machine_rows, data.employee_rows, data.ticket_rows):
            self.assertIn(
                legacy.pk,
                [block.session_id for row in rows for block in row.blocks],
            )

    # --- geometry --------------------------------------------------------

    def test_block_geometry_is_expressed_as_percentages_with_a_dot(self):
        make_session(
            self.ticket, start=self.at(8), end=self.at(11), resources=[self.operator]
        )

        block = self.rows_by_label(self.context().build())["Ana Ruiz"].blocks[0]

        self.assertEqual(block.left, "0.0000")
        self.assertNotIn(",", block.width)
        self.assertAlmostEqual(float(block.width), 100 * 180 / 600, places=3)

    def test_now_line_is_absent_when_the_window_is_in_the_past(self):
        data = self.context().build()

        self.assertIsNone(data.now_pct)

    def test_an_unscheduled_company_reports_it_rather_than_rendering_blank(self):
        company = make_company(name="Sin horario")

        data = self.context(company=company).build()

        self.assertFalse(data.has_schedule)
        self.assertTrue(data.window.is_empty)

    # --- state ------------------------------------------------------------

    def test_stage_state_is_read_from_production_item_planning(self):
        ProductionItem.objects.create(
            ticket=self.ticket,
            company=self.company,
            production_stages_order=[STAGE_CUT],
            planning={STAGE_CUT: {"status": ProductionItemStatus.DONE}},
        )
        make_session(
            self.ticket,
            stage=STAGE_CUT,
            start=self.at(9),
            end=self.at(11),
            resources=[self.operator],
        )

        block = self.rows_by_label(self.context().build())["Ana Ruiz"].blocks[0]

        self.assertEqual(block.state, STATE_DONE)
        self.assertTrue(block.is_done)
        self.assertEqual(block.border_status, "done")
        self.assertFalse(block.is_late)  # finished work is never late

    def test_any_item_in_progress_makes_the_stage_running(self):
        ProductionItem.objects.create(
            ticket=self.ticket,
            company=self.company,
            planning={STAGE_CUT: {"status": ProductionItemStatus.DONE}},
        )
        ProductionItem.objects.create(
            ticket=self.ticket,
            company=self.company,
            planning={STAGE_CUT: {"status": ProductionItemStatus.IN_PROGRESS}},
        )
        make_session(
            self.ticket,
            stage=STAGE_CUT,
            start=self.at(9),
            end=self.at(11),
            resources=[self.operator],
        )

        block = self.rows_by_label(self.context().build())["Ana Ruiz"].blocks[0]

        self.assertEqual(block.state, STATE_RUNNING)
        self.assertEqual(block.border_status, "running")

    def test_an_unfinished_session_in_the_past_is_late(self):
        # A Monday two weeks before the fixture window, so it is always behind
        # "now" without pinning the test to a clock.
        past = MONDAY - timedelta(days=14)
        start = past.replace(hour=9, tzinfo=self.tz)
        make_session(
            self.ticket,
            start=start,
            end=start + timedelta(hours=2),
            resources=[self.operator],
        )

        data = self.context(
            start=past.replace(hour=0, tzinfo=self.tz),
            end=past.replace(hour=23, minute=59, tzinfo=self.tz),
        ).build()
        block = self.rows_by_label(data)["Ana Ruiz"].blocks[0]

        self.assertEqual(block.state, STATE_PLANNED)
        self.assertTrue(block.is_late)
        self.assertEqual(block.border_status, "overdue")
        self.assertFalse(block.has_conflicts)
        self.assertIn(str(_("Overdue")), block.title)
        # Regression: Conflict.message built with gettext_lazy() is not a str
        # instance, and title's str.join() used to blow up on exactly this.
        self.assertIsInstance(block.title, str)

    def test_an_in_progress_stage_stays_blue_after_its_planned_end(self):
        ProductionItem.objects.create(
            ticket=self.ticket,
            company=self.company,
            planning={STAGE_CUT: {"status": ProductionItemStatus.IN_PROGRESS}},
        )
        past = MONDAY - timedelta(days=14)
        start = past.replace(hour=9, tzinfo=self.tz)
        make_session(
            self.ticket,
            stage=STAGE_CUT,
            start=start,
            end=start + timedelta(hours=2),
            resources=[self.operator],
        )

        data = self.context(
            start=past.replace(hour=0, tzinfo=self.tz),
            end=past.replace(hour=23, minute=59, tzinfo=self.tz),
        ).build()
        block = self.rows_by_label(data)["Ana Ruiz"].blocks[0]

        self.assertTrue(block.is_late)
        self.assertTrue(block.is_running)
        self.assertEqual(block.border_status, "running")
        self.assertFalse(block.has_conflicts)
        self.assertIn(str(_("In progress")), block.title)
        self.assertNotIn(str(_("Overdue")), block.title)

    def test_a_combined_stage_hides_its_status_border(self):
        other_ticket = make_ticket(self.company)
        make_session(
            self.ticket,
            start=self.at(9),
            end=self.at(11),
            resources=[self.operator],
        )
        make_session(
            other_ticket,
            start=self.at(10),
            end=self.at(12),
            resources=[self.operator],
        )

        blocks = self.rows_by_label(self.context().build())["Ana Ruiz"].blocks

        self.assertTrue(all(block.has_conflicts for block in blocks))
        self.assertTrue(all(block.is_combined for block in blocks))
        self.assertEqual({block.border_status for block in blocks}, {""})

    def test_a_future_session_is_not_late(self):
        make_session(
            self.ticket, start=self.at(9), end=self.at(11), resources=[self.operator]
        )

        block = self.rows_by_label(self.context().build())["Ana Ruiz"].blocks[0]

        self.assertFalse(block.is_late)

    # --- ticket rows ------------------------------------------------------

    def test_ticket_rows_hold_the_stages_in_sequence(self):
        make_session(
            self.ticket,
            stage=STAGE_CUT,
            start=self.at(8),
            end=self.at(9),
            resources=[self.operator],
            sequence=0,
        )
        make_session(
            self.ticket,
            stage=STAGE_PAINT,
            start=self.at(9),
            end=self.at(11),
            resources=[self.machine],
            sequence=1,
        )

        data = self.context().build()

        self.assertEqual(len(data.ticket_rows), 1)
        row = data.ticket_rows[0]
        self.assertEqual(row.label, f"OP-{self.ticket.pk}")
        self.assertEqual([b.stage for b in row.blocks], [STAGE_CUT, STAGE_PAINT])
        self.assertTrue(all(block.height_pct == 100.0 for block in row.blocks))
        self.assertTrue(all(block.show_label for block in row.blocks))

    def test_ticket_rows_keep_the_ticket_colour(self):
        make_session(
            self.ticket,
            stage=STAGE_CUT,
            start=self.at(8),
            end=self.at(9),
            resources=[self.operator],
            sequence=0,
        )
        make_session(
            self.ticket,
            stage=STAGE_PAINT,
            start=self.at(9),
            end=self.at(11),
            resources=[self.machine],
            sequence=1,
        )

        data = self.context().build()
        colors = [b.color for b in data.ticket_rows[0].blocks]

        self.assertEqual(colors, [ticket_color(self.ticket.pk)] * 2)
        # The resource board uses that same global ticket colour.
        board_block = self.rows_by_label(data)["Ana Ruiz"].blocks[0]
        self.assertEqual(board_block.color, ticket_color(self.ticket.pk))

    def test_recolouring_does_not_disturb_the_resource_rows(self):
        make_session(
            self.ticket,
            stage=STAGE_CUT,
            start=self.at(8),
            end=self.at(9),
            resources=[self.operator],
        )

        data = self.context().build()

        board = self.rows_by_label(data)["Ana Ruiz"].blocks[0]
        ticket = data.ticket_rows[0].blocks[0]
        self.assertIsNot(board, ticket)
        self.assertFalse(board.is_displaced)

    # --- query budget -----------------------------------------------------

    QUERY_BUDGET = 9

    def _touch_everything(self, data):
        """Read every attribute a template reads, to surface lazy loads."""
        for row in data.machine_rows + data.employee_rows:
            for block in row.blocks:
                _ = (
                    block.title,
                    block.left,
                    block.width,
                    block.text_color,
                    block.resource_labels,
                    block.ticket_label,
                )
        for group in data.stage_groups:
            _ = group.blocks
            for row in group.operator_rows:
                _ = row.blocks
            if group.unassigned_machine_row is not None:
                _ = group.unassigned_machine_row.blocks
            for machine_group in group.machine_groups:
                _ = machine_group.blocks
                for row in machine_group.operator_rows:
                    _ = row.blocks
        for row in data.ticket_rows:
            for block in row.blocks:
                _ = (block.title, block.stage_abbr, block.left)

    def _populate(self, tickets: int, extra_resources: int = 0):
        for index in range(extra_resources):
            resource_for(make_machine(self.company, name=f"AUX-{index:02d}"))
        for index in range(tickets):
            ticket = make_ticket(self.company)
            make_session(
                ticket,
                start=self.at(8) + timedelta(minutes=15 * index),
                end=self.at(9) + timedelta(minutes=15 * index),
                resources=[self.operator, self.machine],
            )

    def test_building_the_board_has_a_fixed_query_cost(self):
        """Guards against re-introducing per-block queries in the templates."""
        self._populate(tickets=6)

        with self.assertNumQueries(self.QUERY_BUDGET):
            self._touch_everything(self.context().build())

    def test_query_cost_does_not_grow_with_the_data(self):
        """Same budget with four times the board: constant, not proportional."""
        self._populate(tickets=24, extra_resources=10)

        with self.assertNumQueries(self.QUERY_BUDGET):
            self._touch_everything(self.context().build())

    def test_direct_project_files_do_not_add_queries_per_ticket(self):
        client = Client.objects.create(company=self.company, name="Test client")
        project = ProjectFile.objects.create(
            company=self.company,
            client=client,
            name="Test project",
            reference="EXP-TEST",
        )
        for index in range(6):
            ticket = make_ticket(self.company)
            ticket.project_file = project
            ticket.save(update_fields=["project_file"])
            make_session(
                ticket,
                start=self.at(8) + timedelta(minutes=15 * index),
                end=self.at(9) + timedelta(minutes=15 * index),
                resources=[self.operator, self.machine],
            )

        with self.assertNumQueries(self.QUERY_BUDGET):
            self._touch_everything(self.context().build())


class ColorHelpersTest(TestCase):
    def test_ticket_colour_is_stable_and_deterministic(self):
        self.assertEqual(ticket_color(42), ticket_color(42))
        self.assertNotEqual(ticket_color(1), ticket_color(2))

    def test_stage_abbreviation(self):
        self.assertEqual(stage_abbreviation("corte"), "COR")
        self.assertEqual(stage_abbreviation("montaje y soldadura"), "MYS")
        self.assertEqual(stage_abbreviation("cnc"), "CNC")

    def test_text_colour_follows_luminance(self):
        self.assertEqual(text_color_for("#ffffff"), "#0e1013")
        self.assertEqual(text_color_for("#111111"), "#ffffff")
        self.assertEqual(text_color_for("nonsense"), "#0e1013")



class OverlapLanesTest(TestCase):
    """Sessions sharing a row divide it into fixed lanes and lose their labels."""

    @classmethod
    def setUpTestData(cls):
        cls.company = make_company()
        add_schedule(cls.company)
        cls.tz = timezone.get_current_timezone()
        cls.operator = resource_for(
            make_employee(cls.company, first_name="Ana", last_name="Ruiz")
        )
        cls.ticket = make_ticket(cls.company)

    def at(self, hour: int) -> datetime:
        return MONDAY.replace(hour=hour, tzinfo=self.tz)

    def operator_blocks(self):
        data = ProductionScheduleContext(
            company=self.company, start=self.at(0), end=self.at(23)
        ).build()
        row = next(row for row in data.employee_rows if row.label == "Ana Ruiz")
        return {block.session_id: block for block in row.blocks}

    def test_a_session_alone_in_its_row_is_full_height_and_labelled(self):
        session = make_session(
            self.ticket, start=self.at(9), end=self.at(11), resources=[self.operator]
        )

        block = self.operator_blocks()[session.pk]

        self.assertEqual(block.height_pct, 100.0)
        self.assertTrue(block.show_label)

    def test_two_sessions_at_once_take_half_the_row_each_and_lose_their_labels(self):
        first = make_session(
            self.ticket, start=self.at(9), end=self.at(11), resources=[self.operator]
        )
        second = make_session(
            self.ticket,
            stage=STAGE_WELD,
            start=self.at(9),
            end=self.at(11),
            resources=[self.operator],
        )

        blocks = self.operator_blocks()

        for pk in (first.pk, second.pk):
            self.assertEqual(blocks[pk].height_pct, 50.0)
            self.assertFalse(blocks[pk].show_label)
        self.assertEqual(
            sorted(blocks[pk].top_pct for pk in (first.pk, second.pk)), [0.0, 50.0]
        )

    def test_a_lane_holds_even_once_the_session_is_briefly_alone_again(self):
        """The old per-segment layout changed a block's own height mid-flight
        as neighbours came and went, which read as a glitch rather than as
        information. The fixed-lane layout deliberately does not: a session
        keeps one lane, and one label state, for its whole length."""
        long_session = make_session(
            self.ticket, start=self.at(9), end=self.at(13), resources=[self.operator]
        )
        make_session(
            self.ticket, start=self.at(9), end=self.at(11), resources=[self.operator]
        )

        block = self.operator_blocks()[long_session.pk]

        self.assertEqual(block.height_pct, 50.0)
        self.assertFalse(block.show_label)

    def test_a_session_outside_the_overlap_keeps_the_full_row(self):
        make_session(
            self.ticket, start=self.at(9), end=self.at(10), resources=[self.operator]
        )
        make_session(
            self.ticket,
            stage=STAGE_WELD,
            start=self.at(9),
            end=self.at(10),
            resources=[self.operator],
        )
        later = make_session(
            self.ticket,
            stage=STAGE_PAINT,
            start=self.at(14),
            end=self.at(15),
            resources=[self.operator],
        )

        block = self.operator_blocks()[later.pk]

        self.assertEqual(block.height_pct, 100.0)
        self.assertTrue(block.show_label)
