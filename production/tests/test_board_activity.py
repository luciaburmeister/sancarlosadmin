from datetime import date, datetime, timedelta, timezone as dt_timezone

from django.contrib.contenttypes.models import ContentType
from django.test import TestCase

from company.models import Company, Employee
from configurator.models import Configuration, ProductTemplate
from production.models import (
    ProductionItem,
    ProductionItemSession,
    ProductionItemSessionStatus,
    ProductionTicket,
)
from production.services.board_activity import build_week_activity

STAGE = "corte"


class BuildWeekActivityTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name="Test Co")
        cls.product = ProductTemplate.objects.create(
            name="Ventana", company=cls.company, production_stages=[STAGE],
        )
        cls.jose = Employee.objects.create(
            company=cls.company, first_name="Jose", last_name="Murillo",
            hired_at=date(2020, 1, 1), can_work_on_production=True,
        )
        cls.maria = Employee.objects.create(
            company=cls.company, first_name="Maria", last_name="Garcia",
            hired_at=date(2020, 1, 1), can_work_on_production=True,
        )
        cls.config = Configuration.objects.create(company=cls.company, product=cls.product)
        ct = ContentType.objects.get_for_model(Configuration)
        cls.ticket = ProductionTicket.objects.create(
            company=cls.company, content_type=ct, object_id=cls.config.id,
        )

        # Displayed week: Monday 2026-09-14 .. next Monday (exclusive).
        cls.range_start = datetime(2026, 9, 14, tzinfo=dt_timezone.utc)
        cls.range_end = cls.range_start + timedelta(days=7)

    def _item(self, status: str) -> ProductionItem:
        return ProductionItem.objects.create(
            ticket=self.ticket, company=self.company,
            planning={STAGE: {"status": status}},
        )

    def _session(self, item, employee, start, end=None, status=ProductionItemSessionStatus.IN_PROGRESS, comments=None):
        return ProductionItemSession.objects.create(
            production_item=item, company=self.company, production_stage=STAGE,
            status=status, start_time=start, end_time=end, created_by=employee,
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

    def test_cancelled_only_session_produces_no_entry(self):
        item = self._item("pending")
        self._session(
            item, self.jose, datetime(2026, 9, 15, 9, 0, tzinfo=dt_timezone.utc),
            end=datetime(2026, 9, 15, 9, 30, tzinfo=dt_timezone.utc),
            status=ProductionItemSessionStatus.CANCELLED,
        )

        activity = build_week_activity(self.company, self.range_start, self.range_end)
        self.assertEqual(activity, [])

    def test_active_wins_over_paused_across_items_of_same_ticket_stage(self):
        active_item = self._item("in_progress")
        paused_item = self._item("in_progress")
        self._session(active_item, self.jose, datetime(2026, 9, 15, 9, 0, tzinfo=dt_timezone.utc))
        self._session(
            paused_item, self.maria, datetime(2026, 9, 15, 8, 0, tzinfo=dt_timezone.utc),
            end=datetime(2026, 9, 15, 8, 30, tzinfo=dt_timezone.utc),
        )

        activity = build_week_activity(self.company, self.range_start, self.range_end)

        self.assertEqual(len(activity), 1)
        self.assertEqual(activity[0]["kind"], "active")

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
