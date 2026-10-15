from datetime import datetime, timedelta, timezone as dt_timezone

from django.test import TestCase

from production.models import ProductionItem, ProductionItemSessionStatus
from production.services.board_activity import build_week_activity
from production.tests.factories import make_company, make_employee, make_item, make_item_session, make_product, make_ticket

STAGE = "corte"


class BuildWeekActivityTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = make_company(stages=[])
        cls.product = make_product(cls.company, name="Ventana", stages=[STAGE])
        cls.jose = make_employee(cls.company, first_name="Jose", last_name="Murillo", stages=[])
        cls.maria = make_employee(cls.company, first_name="Maria", last_name="Garcia", stages=[])
        cls.ticket = make_ticket(cls.company, stages=[STAGE], product=cls.product, bom=False)

        # Displayed week: Monday 2026-09-14 .. next Monday (exclusive).
        cls.range_start = datetime(2026, 9, 14, tzinfo=dt_timezone.utc)
        cls.range_end = cls.range_start + timedelta(days=7)

    def _item(self, status: str) -> ProductionItem:
        return make_item(
            self.ticket, stages=[STAGE], planning={STAGE: {"status": status}}, production_stages_order=[],
        )

    def _session(self, item, employee, start, end=None, status=ProductionItemSessionStatus.IN_PROGRESS, comments=None):
        return make_item_session(
            item, stage=STAGE, employee=employee, start=start, end=end, status=status,
            comment_entries=comments or [],
        )

    def test_open_session_with_in_progress_status_is_active(self):
        item = self._item("in_progress")
        self._session(item, self.jose, datetime(2026, 9, 15, 9, 0, tzinfo=dt_timezone.utc))

        activity = build_week_activity(self.company, self.range_start, self.range_end)

        self.assertEqual(len(activity), 1)
        entry = activity[0]
        self.assertEqual(entry["kind"], "active")
        self.assertEqual(entry["production_ticket_id"], self.ticket.id)
        self.assertEqual(entry["date"], "2026-09-15")
        self.assertEqual(entry["active_workers"], [{"id": self.jose.id, "full_name": self.jose.full_name}])
        self.assertIsNone(entry["end_at"])

    def test_active_after_resume_splits_into_separate_paused_and_active_stints(self):
        """A task started, paused, and resumed must show as two separate
        entries — the earlier paused stint and the current active one — not
        one span covering the gap, which would overlap on the board with
        whatever the worker did during the pause."""
        item = self._item("in_progress")
        self._session(
            item, self.jose,
            datetime(2026, 9, 15, 8, 0, tzinfo=dt_timezone.utc),
            end=datetime(2026, 9, 15, 9, 0, tzinfo=dt_timezone.utc),
        )
        self._session(item, self.jose, datetime(2026, 9, 15, 14, 0, tzinfo=dt_timezone.utc))

        activity = build_week_activity(self.company, self.range_start, self.range_end)

        self.assertEqual(len(activity), 2)
        by_kind = {e["kind"]: e for e in activity}
        self.assertEqual(by_kind["paused"]["start_at"], "2026-09-15T08:00:00+00:00")
        self.assertEqual(by_kind["paused"]["end_at"], "2026-09-15T09:00:00+00:00")
        self.assertEqual(by_kind["active"]["start_at"], "2026-09-15T14:00:00+00:00")
        self.assertIsNone(by_kind["active"]["end_at"])

    def test_in_progress_no_open_session_is_paused(self):
        item = self._item("in_progress")
        self._session(
            item, self.jose,
            datetime(2026, 9, 15, 9, 0, tzinfo=dt_timezone.utc),
            end=datetime(2026, 9, 15, 10, 0, tzinfo=dt_timezone.utc),
        )

        activity = build_week_activity(self.company, self.range_start, self.range_end)

        self.assertEqual(len(activity), 1)
        self.assertEqual(activity[0]["kind"], "paused")
        self.assertEqual(activity[0]["active_workers"], [])

    def test_all_items_done_is_done(self):
        item = self._item("done")
        end = datetime(2026, 9, 16, 12, 0, tzinfo=dt_timezone.utc)
        self._session(
            item, self.jose, datetime(2026, 9, 16, 9, 0, tzinfo=dt_timezone.utc),
            end=end, status=ProductionItemSessionStatus.DONE,
        )

        activity = build_week_activity(self.company, self.range_start, self.range_end)

        self.assertEqual(len(activity), 1)
        entry = activity[0]
        self.assertEqual(entry["kind"], "done")
        self.assertEqual(entry["date"], "2026-09-16")
        self.assertEqual(entry["end_at"], end.isoformat())

    def test_done_after_earlier_pause_keeps_both_stints_marked_done(self):
        """A task worked, paused, and resumed to completion must show both
        stints, each with its own real time range and both marked done —
        not one span covering the gap, and not just the finishing stint."""
        item = self._item("done")
        self._session(
            item, self.jose,
            datetime(2026, 9, 16, 8, 0, tzinfo=dt_timezone.utc),
            end=datetime(2026, 9, 16, 9, 0, tzinfo=dt_timezone.utc),
        )
        finish = datetime(2026, 9, 16, 15, 0, tzinfo=dt_timezone.utc)
        self._session(
            item, self.jose, datetime(2026, 9, 16, 14, 0, tzinfo=dt_timezone.utc),
            end=finish, status=ProductionItemSessionStatus.DONE,
        )

        activity = build_week_activity(self.company, self.range_start, self.range_end)

        self.assertEqual(len(activity), 2)
        self.assertTrue(all(e["kind"] == "done" for e in activity))
        by_start = {e["start_at"]: e for e in activity}
        self.assertEqual(
            by_start["2026-09-16T08:00:00+00:00"]["end_at"], "2026-09-16T09:00:00+00:00",
        )
        self.assertEqual(by_start["2026-09-16T14:00:00+00:00"]["end_at"], finish.isoformat())

    def test_done_stint_from_a_prior_week_keeps_its_own_date_not_clamped(self):
        """An earlier stint that finished before the displayed week must show
        on its real date, not get pulled onto this week's Monday — otherwise
        two unrelated stints from different weeks land on the same fake day
        and look like they overlap."""
        item = self._item("done")
        self._session(
            item, self.jose,
            datetime(2026, 9, 7, 8, 0, tzinfo=dt_timezone.utc),
            end=datetime(2026, 9, 7, 9, 0, tzinfo=dt_timezone.utc),
        )
        finish = datetime(2026, 9, 16, 15, 0, tzinfo=dt_timezone.utc)
        self._session(
            item, self.jose, datetime(2026, 9, 16, 14, 0, tzinfo=dt_timezone.utc),
            end=finish, status=ProductionItemSessionStatus.DONE,
        )

        activity = build_week_activity(self.company, self.range_start, self.range_end)

        self.assertEqual(len(activity), 2)
        by_start = {e["start_at"]: e for e in activity}
        self.assertEqual(by_start["2026-09-07T08:00:00+00:00"]["date"], "2026-09-07")
        self.assertEqual(by_start["2026-09-16T14:00:00+00:00"]["date"], "2026-09-16")

    def test_cancelled_only_session_produces_no_entry(self):
        item = self._item("pending")
        self._session(
            item, self.jose, datetime(2026, 9, 15, 9, 0, tzinfo=dt_timezone.utc),
            end=datetime(2026, 9, 15, 9, 30, tzinfo=dt_timezone.utc),
            status=ProductionItemSessionStatus.CANCELLED,
        )

        activity = build_week_activity(self.company, self.range_start, self.range_end)
        self.assertEqual(activity, [])

    def test_active_and_paused_items_of_same_ticket_stage_show_as_separate_entries(self):
        active_item = self._item("in_progress")
        paused_item = self._item("in_progress")
        self._session(active_item, self.jose, datetime(2026, 9, 15, 9, 0, tzinfo=dt_timezone.utc))
        self._session(
            paused_item, self.maria, datetime(2026, 9, 15, 8, 0, tzinfo=dt_timezone.utc),
            end=datetime(2026, 9, 15, 8, 30, tzinfo=dt_timezone.utc),
        )

        activity = build_week_activity(self.company, self.range_start, self.range_end)

        self.assertEqual(len(activity), 2)
        kinds = {e["kind"] for e in activity}
        self.assertEqual(kinds, {"active", "paused"})

    def test_multi_worker_active_workers_lists_everyone_currently_open(self):
        item_a = self._item("in_progress")
        item_b = self._item("in_progress")
        self._session(item_a, self.jose, datetime(2026, 9, 15, 9, 0, tzinfo=dt_timezone.utc))
        self._session(item_b, self.maria, datetime(2026, 9, 15, 9, 15, tzinfo=dt_timezone.utc))

        activity = build_week_activity(self.company, self.range_start, self.range_end)

        self.assertEqual(len(activity), 1)
        worker_ids = {w["id"] for w in activity[0]["active_workers"]}
        self.assertEqual(worker_ids, {self.jose.id, self.maria.id})

    def test_still_open_session_started_before_window_is_clamped_to_week_start(self):
        item = self._item("in_progress")
        self._session(item, self.jose, datetime(2026, 9, 10, 9, 0, tzinfo=dt_timezone.utc))  # before the window

        activity = build_week_activity(self.company, self.range_start, self.range_end)

        self.assertEqual(len(activity), 1)
        self.assertEqual(activity[0]["date"], "2026-09-14")  # clamped to Monday

    def test_paused_session_closed_long_before_window_still_shows(self):
        """A stage paused days (or weeks) ago and never resumed must keep
        showing up on the board — it's a live "in_progress" item, not a
        finished one, no matter how stale its last session is. Found via a
        real repro: seeding a session closed 6 days before the displayed
        week silently made it vanish before this was fixed."""
        item = self._item("in_progress")
        self._session(
            item, self.jose,
            datetime(2026, 8, 1, 9, 0, tzinfo=dt_timezone.utc),
            end=datetime(2026, 8, 1, 9, 45, tzinfo=dt_timezone.utc),
        )

        activity = build_week_activity(self.company, self.range_start, self.range_end)

        self.assertEqual(len(activity), 1)
        self.assertEqual(activity[0]["kind"], "paused")
        self.assertEqual(activity[0]["date"], "2026-09-14")  # clamped to Monday

    def test_session_entirely_before_window_is_excluded(self):
        item = self._item("done")
        self._session(
            item, self.jose, datetime(2026, 9, 1, 9, 0, tzinfo=dt_timezone.utc),
            end=datetime(2026, 9, 1, 10, 0, tzinfo=dt_timezone.utc),
            status=ProductionItemSessionStatus.DONE,
        )

        activity = build_week_activity(self.company, self.range_start, self.range_end)
        self.assertEqual(activity, [])

    def test_comments_are_collected(self):
        item = self._item("in_progress")
        self._session(
            item, self.jose, datetime(2026, 9, 15, 9, 0, tzinfo=dt_timezone.utc),
            comments=[{"text": "Falta material", "at": "2026-09-15T09:05:00Z"}],
        )

        activity = build_week_activity(self.company, self.range_start, self.range_end)

        self.assertEqual(len(activity[0]["session_comments"]), 1)
        self.assertEqual(activity[0]["session_comments"][0]["comment"], "Falta material")
        self.assertEqual(activity[0]["session_comments"][0]["employee_name"], self.jose.full_name)
