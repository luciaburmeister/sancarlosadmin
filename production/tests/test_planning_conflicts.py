from __future__ import annotations

import unicodedata
from datetime import datetime, timedelta

from django.test import TestCase
from django.utils import timezone

from company.models import WeekdayChoices
from production.services.planning_conflicts import (
    BLOCK,
    WARN,
    ConflictChecker,
    ConflictCode,
    SessionCandidate,
)
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

MONDAY = datetime(2026, 9, 14, 8, 0)
SATURDAY = datetime(2026, 9, 12, 8, 0)


class ConflictCheckerTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = make_company()
        add_schedule(cls.company)  # Mon-Fri 08:00-14:00, 15:00-18:00
        cls.tz = timezone.get_current_timezone()

        cls.ana = make_employee(cls.company, first_name="Ana")
        cls.machine = make_machine(cls.company, name="CNC-01")
        cls.operator = resource_for(cls.ana)
        cls.cnc = resource_for(cls.machine)

        cls.ticket = make_ticket(cls.company)

    def at(self, base: datetime, hour: int, minute: int = 0) -> datetime:
        return base.replace(hour=hour, minute=minute, tzinfo=self.tz)

    def checker(self):
        return ConflictChecker(self.company)

    def candidate(self, *, start=None, end=None, resources=None, **kwargs):
        return SessionCandidate(
            company_id=self.company.pk,
            stage=kwargs.pop("stage", STAGE_CUT),
            start=start or self.at(MONDAY, 9),
            end=end or self.at(MONDAY, 11),
            resource_ids=[r.pk for r in (resources if resources is not None else [self.operator])],
            ticket_id=kwargs.pop("ticket_id", self.ticket.pk),
            **kwargs,
        )

    def codes(self, conflicts):
        return {c.code for c in conflicts}

    def only(self, conflicts, code):
        matches = [c for c in conflicts if c.code == code]
        self.assertEqual(len(matches), 1, f"expected one {code}, got {self.codes(conflicts)}")
        return matches[0]

    # --- clean case ------------------------------------------------------

    def test_a_well_placed_session_has_no_conflicts(self):
        self.assertEqual(self.checker().check(self.candidate()), [])

    # --- integrity (blocking) --------------------------------------------

    def test_inverted_interval_blocks(self):
        conflicts = self.checker().check(
            self.candidate(start=self.at(MONDAY, 11), end=self.at(MONDAY, 9))
        )

        conflict = self.only(conflicts, ConflictCode.INVALID_INTERVAL)
        self.assertEqual(conflict.severity, BLOCK)
        # No point running the rest once the interval is nonsense.
        self.assertEqual(len(conflicts), 1)

    def test_locked_session_blocks(self):
        conflicts = self.checker().check(self.candidate(locked=True))

        self.assertEqual(self.only(conflicts, ConflictCode.LOCKED_SESSION).severity, BLOCK)

    def test_resource_from_another_company_blocks(self):
        other = make_company(name="Otra SL")
        stranger = resource_for(make_employee(other, first_name="Foráneo"))

        conflicts = self.checker().check(self.candidate(resources=[stranger]))

        conflict = self.only(conflicts, ConflictCode.CROSS_COMPANY)
        self.assertEqual(conflict.severity, BLOCK)
        self.assertEqual(conflict.resource_id, stranger.pk)

    # --- calendar --------------------------------------------------------

    def test_saturday_is_a_non_working_day(self):
        conflicts = self.checker().check(
            self.candidate(start=self.at(SATURDAY, 9), end=self.at(SATURDAY, 11))
        )

        conflict = self.only(conflicts, ConflictCode.NON_WORKING_DAY)
        self.assertEqual(conflict.severity, WARN)
        self.assertEqual(conflict.detail["open_minutes"], 0)

    def test_running_past_closing_time_is_outside_working_hours(self):
        conflicts = self.checker().check(
            self.candidate(start=self.at(MONDAY, 17), end=self.at(MONDAY, 19))
        )

        conflict = self.only(conflicts, ConflictCode.OUTSIDE_WORKING_HOURS)
        self.assertEqual(conflict.detail["open_minutes"], 60)
        self.assertEqual(conflict.detail["wall_minutes"], 120)

    def test_spanning_the_lunch_break_is_allowed(self):
        """13:00-16:00 is 3 wall hours but 2 open ones, and that is fine:
        the planner asked for 2 hours of work and lunch fell in the middle."""
        conflicts = self.checker().check(
            self.candidate(start=self.at(MONDAY, 13), end=self.at(MONDAY, 16))
        )

        self.assertNotIn(ConflictCode.OUTSIDE_WORKING_HOURS, self.codes(conflicts))

    def test_starting_during_the_lunch_break_is_reported(self):
        conflicts = self.checker().check(
            self.candidate(start=self.at(MONDAY, 14, 30), end=self.at(MONDAY, 16))
        )

        self.assertIn(ConflictCode.OUTSIDE_WORKING_HOURS, self.codes(conflicts))

    # --- skills ----------------------------------------------------------

    def test_resource_without_the_stage_is_flagged(self):
        specialist = resource_for(
            make_employee(
                self.company, first_name="Solo", last_name="Corte", stages=[STAGE_CUT]
            )
        )

        conflicts = self.checker().check(
            self.candidate(stage=STAGE_PAINT, resources=[specialist])
        )

        conflict = self.only(conflicts, ConflictCode.RESOURCE_NOT_SKILLED)
        self.assertEqual(conflict.severity, WARN)
        self.assertEqual(conflict.resource_id, specialist.pk)

    def test_empty_stage_list_means_unrestricted(self):
        generalist = resource_for(
            make_employee(self.company, first_name="Todo", last_name="Terreno", stages=[])
        )

        conflicts = self.checker().check(
            self.candidate(stage=STAGE_PAINT, resources=[generalist])
        )

        self.assertNotIn(ConflictCode.RESOURCE_NOT_SKILLED, self.codes(conflicts))

    def test_stage_names_are_compared_nfc_normalized(self):
        """macOS clients send decomposed accents; the two forms must match."""
        composed = unicodedata.normalize("NFC", "mecanización")
        decomposed = unicodedata.normalize("NFD", "mecanización")
        self.assertNotEqual(composed, decomposed)

        worker = resource_for(
            make_employee(
                self.company, first_name="Nfc", last_name="Test", stages=[composed]
            )
        )

        conflicts = self.checker().check(
            self.candidate(stage=decomposed, resources=[worker])
        )

        self.assertNotIn(ConflictCode.RESOURCE_NOT_SKILLED, self.codes(conflicts))

    def test_employee_not_flagged_for_production_is_reported(self):
        office = resource_for(
            make_employee(
                self.company,
                first_name="Admin",
                last_name="Oficina",
                can_work_on_production=False,
            )
        )

        conflicts = self.checker().check(self.candidate(resources=[office]))

        self.assertIn(ConflictCode.RESOURCE_NOT_SKILLED, self.codes(conflicts))

    # --- overlap ---------------------------------------------------------

    def test_overlap_beyond_parallel_capacity_is_reported(self):
        make_session(
            self.ticket,
            start=self.at(MONDAY, 10),
            end=self.at(MONDAY, 12),
            resources=[self.operator],
        )

        conflicts = self.checker().check(self.candidate(ticket_id=None))

        conflict = self.only(conflicts, ConflictCode.RESOURCE_OVERLAP)
        self.assertEqual(conflict.detail, {"busy": 1, "parallel_capacity": 1})

    def test_parallel_capacity_two_absorbs_one_overlap(self):
        self.cnc.parallel_capacity = 2
        self.cnc.save(update_fields=["parallel_capacity"])
        make_session(
            self.ticket,
            start=self.at(MONDAY, 10),
            end=self.at(MONDAY, 12),
            resources=[self.cnc],
        )

        conflicts = self.checker().check(
            self.candidate(resources=[self.cnc], ticket_id=None)
        )

        self.assertNotIn(ConflictCode.RESOURCE_OVERLAP, self.codes(conflicts))

    def test_a_session_does_not_overlap_itself(self):
        session = make_session(
            self.ticket,
            start=self.at(MONDAY, 9),
            end=self.at(MONDAY, 11),
            resources=[self.operator],
        )

        conflicts = self.checker().check(SessionCandidate.from_session(session))

        self.assertNotIn(ConflictCode.RESOURCE_OVERLAP, self.codes(conflicts))

    def test_overlap_counts_secondary_resources_too(self):
        """The machine half of a machine+operator session must block the machine."""
        make_session(
            self.ticket,
            start=self.at(MONDAY, 10),
            end=self.at(MONDAY, 12),
            resources=[self.operator, self.cnc],
        )

        conflicts = self.checker().check(
            self.candidate(resources=[self.cnc], ticket_id=None)
        )

        self.assertEqual(
            self.only(conflicts, ConflictCode.RESOURCE_OVERLAP).resource_id, self.cnc.pk
        )

    # --- daily capacity ---------------------------------------------------

    def test_daily_capacity_counts_open_minutes_only(self):
        self.cnc.daily_capacity_minutes = 120
        self.cnc.save(update_fields=["daily_capacity_minutes"])

        # 13:00-16:00 spans lunch: 120 open minutes, exactly the capacity.
        ok = self.checker().check(
            self.candidate(
                start=self.at(MONDAY, 13),
                end=self.at(MONDAY, 16),
                resources=[self.cnc],
                ticket_id=None,
            )
        )
        self.assertNotIn(ConflictCode.DAILY_CAPACITY_EXCEEDED, self.codes(ok))

        over = self.checker().check(
            self.candidate(
                start=self.at(MONDAY, 8),
                end=self.at(MONDAY, 11),
                resources=[self.cnc],
                ticket_id=None,
            )
        )
        conflict = self.only(over, ConflictCode.DAILY_CAPACITY_EXCEEDED)
        self.assertEqual(conflict.detail["capacity"], 120)
        self.assertEqual(conflict.detail["candidate"], 180)

    # --- stage order ------------------------------------------------------

    def test_starting_before_the_previous_stage_ends_is_reported(self):
        previous = make_session(
            self.ticket,
            stage=STAGE_CUT,
            start=self.at(MONDAY, 10),
            end=self.at(MONDAY, 12),
            resources=[self.operator],
            sequence=0,
        )

        conflicts = self.checker().check(
            self.candidate(
                stage=STAGE_PAINT,
                start=self.at(MONDAY, 11),
                end=self.at(MONDAY, 13),
                resources=[self.cnc],
                sequence=1,
            )
        )

        conflict = self.only(conflicts, ConflictCode.STAGE_ORDER_VIOLATION)
        self.assertEqual(conflict.detail["neighbour"], "previous")
        self.assertEqual(conflict.detail["neighbour_session_id"], previous.pk)

    def test_ending_after_the_next_stage_starts_is_reported(self):
        following = make_session(
            self.ticket,
            stage=STAGE_PAINT,
            start=self.at(MONDAY, 11),
            end=self.at(MONDAY, 13),
            resources=[self.cnc],
            sequence=1,
        )

        conflicts = self.checker().check(
            self.candidate(
                stage=STAGE_CUT,
                start=self.at(MONDAY, 9),
                end=self.at(MONDAY, 12),
                sequence=0,
            )
        )

        conflict = self.only(conflicts, ConflictCode.STAGE_ORDER_VIOLATION)
        self.assertEqual(conflict.detail["neighbour"], "next")
        self.assertEqual(conflict.detail["neighbour_session_id"], following.pk)

    def test_stages_in_order_have_no_order_conflict(self):
        make_session(
            self.ticket,
            stage=STAGE_CUT,
            start=self.at(MONDAY, 9),
            end=self.at(MONDAY, 10),
            resources=[self.operator],
            sequence=0,
        )

        conflicts = self.checker().check(
            self.candidate(
                stage=STAGE_PAINT,
                start=self.at(MONDAY, 10),
                end=self.at(MONDAY, 11),
                resources=[self.cnc],
                sequence=1,
            )
        )

        self.assertNotIn(ConflictCode.STAGE_ORDER_VIOLATION, self.codes(conflicts))

    # --- requirements -----------------------------------------------------

    def test_missing_machine_is_reported(self):
        conflicts = self.checker().check_requirements(
            self.candidate(resources=[self.operator]), machine_qty=1, employee_qty=1
        )

        conflict = self.only(conflicts, ConflictCode.REQUIREMENT_UNMET)
        self.assertEqual(conflict.detail, {"kind": "machine", "needed": 1, "assigned": 0})

    def test_machine_plus_operator_satisfies_the_requirement(self):
        conflicts = self.checker().check_requirements(
            self.candidate(resources=[self.operator, self.cnc]),
            machine_qty=1,
            employee_qty=1,
        )

        self.assertEqual(conflicts, [])
