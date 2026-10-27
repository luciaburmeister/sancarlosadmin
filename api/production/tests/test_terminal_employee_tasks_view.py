from datetime import date, datetime, time, timedelta

from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework.test import APIClient

from api.production.views.terminal import _build_task_results
from company.models import Company, CompanyUser, Employee
from configurator.models import Configuration, ConfigurationItem, ConfigurationStatus, ProductTemplate
from production.models import PlannedProductionSession, ProductionItem, ProductionTicket
from production.services.session_scheduler import get_or_create_resource_for_target

User = get_user_model()


class TerminalEmployeeTasksViewPlannedInfoTest(TestCase):
    """The employee task list surfaces the calendar plan (day + order) for
    each stage, so a worker can tell from their own screen what to do and in
    what order — see production.services.planning_status_sync for how a
    session's employee gets attached to the item's roster in the first place."""

    @classmethod
    def setUpTestData(cls):
        # Relative to "today" (not a hardcoded date) because the planned-info
        # lookup filters on planned_start__date__gte=today — a fixed future
        # date would silently become "past" once enough real time passes.
        cls.MONDAY = timezone.localdate() + timedelta(days=5)

        cls.company = Company.objects.create(name="Test Co")

        cls.terminal_user = User.objects.create_user(username="terminal", password="x")
        CompanyUser.objects.create(
            user=cls.terminal_user, company=cls.company, is_active=True,
            can_use_shared_terminal=True,
        )

        cls.worker = Employee.objects.create(
            company=cls.company, first_name="Jose", last_name="Murillo",
            hired_at=date(2020, 1, 1), can_work_on_production=True, is_active=True,
        )
        cls.resource = get_or_create_resource_for_target(cls.worker)

        cls.product = ProductTemplate.objects.create(
            name="Ventana", company=cls.company, production_stages=["corte", "montaje"],
        )
        cls.config_a = Configuration.objects.create(
            company=cls.company, product=cls.product, status=ConfigurationStatus.CONVERTED,
        )
        cls.config_b = Configuration.objects.create(
            company=cls.company, product=cls.product, status=ConfigurationStatus.CONVERTED,
        )
        # active_ticket_items() only keeps items whose ticket's config still
        # has a BOM (source.items) — an empty BOM makes it return [] outright,
        # even for legacy/unlinked items, so each config needs at least one row.
        ConfigurationItem.objects.create(configuration=cls.config_a, unit_price=0)
        ConfigurationItem.objects.create(configuration=cls.config_b, unit_price=0)
        ct = ContentType.objects.get_for_model(Configuration)
        cls.ticket_a = ProductionTicket.objects.create(
            company=cls.company, content_type=ct, object_id=cls.config_a.id,
        )
        cls.ticket_b = ProductionTicket.objects.create(
            company=cls.company, content_type=ct, object_id=cls.config_b.id,
        )
        cls.item_a = ProductionItem.objects.create(
            ticket=cls.ticket_a, company=cls.company,
            planning={"corte": {"status": "pending"}},
        )
        cls.item_b = ProductionItem.objects.create(
            ticket=cls.ticket_b, company=cls.company,
            planning={"montaje": {"status": "pending"}},
        )
        cls.item_a.assigned_employees.add(cls.worker)
        cls.item_b.assigned_employees.add(cls.worker)

    def setUp(self):
        self.client = APIClient()
        self.client.force_authenticate(user=self.terminal_user)

    def _aware(self, d: date, t: time) -> datetime:
        return timezone.make_aware(datetime.combine(d, t))

    def _get(self):
        resp = self.client.get(f"/es/api/production/employees/{self.worker.id}/tasks/")
        self.assertEqual(resp.status_code, 200)
        return {r["next_stage"]["production_item_id"]: r["next_stage"] for r in resp.json()["results"]}

    def test_stage_without_plan_has_null_planned_fields(self):
        stages = self._get()
        self.assertIsNone(stages[self.item_a.id]["planned_date"])
        self.assertIsNone(stages[self.item_a.id]["planned_order"])
        self.assertIsNone(stages[self.item_a.id]["planned_day_total"])

    def test_planned_stage_reports_date_order_and_day_total(self):
        PlannedProductionSession.objects.create(
            production_ticket=self.ticket_a, production_stage="corte",
            planned_start=self._aware(self.MONDAY, time(9, 0)),
            planned_end=self._aware(self.MONDAY, time(10, 0)),
            primary_resource=self.resource,
        )
        PlannedProductionSession.objects.create(
            production_ticket=self.ticket_b, production_stage="montaje",
            planned_start=self._aware(self.MONDAY, time(11, 0)),
            planned_end=self._aware(self.MONDAY, time(12, 0)),
            primary_resource=self.resource,
        )
        stages = self._get()
        self.assertEqual(stages[self.item_a.id]["planned_date"], self.MONDAY.isoformat())
        self.assertEqual(stages[self.item_a.id]["planned_order"], 1)
        self.assertEqual(stages[self.item_a.id]["planned_day_total"], 2)
        self.assertEqual(stages[self.item_b.id]["planned_order"], 2)
        self.assertEqual(stages[self.item_b.id]["planned_day_total"], 2)

    def test_results_are_ordered_by_plan_before_unplanned(self):
        # item_b is planned; item_a is not — the planned one should come first
        # in the response even though item_a's ticket was created first.
        PlannedProductionSession.objects.create(
            production_ticket=self.ticket_b, production_stage="montaje",
            planned_start=self._aware(self.MONDAY, time(9, 0)),
            planned_end=self._aware(self.MONDAY, time(10, 0)),
            primary_resource=self.resource,
        )
        resp = self.client.get(f"/es/api/production/employees/{self.worker.id}/tasks/")
        item_ids = [r["next_stage"]["production_item_id"] for r in resp.json()["results"]]
        self.assertEqual(item_ids, [self.item_b.id, self.item_a.id])

    def test_past_dated_session_is_still_reported_as_planned(self):
        # A stage still pending past its planned day should keep showing its
        # (now overdue) plan, not silently lose it — that's the point of the
        # badge: surfacing that it fell behind.
        past_day = self.MONDAY - timedelta(days=7)
        PlannedProductionSession.objects.create(
            production_ticket=self.ticket_a, production_stage="corte",
            planned_start=self._aware(past_day, time(9, 0)),
            planned_end=self._aware(past_day, time(10, 0)),
            primary_resource=self.resource,
        )
        stages = self._get()
        self.assertEqual(stages[self.item_a.id]["planned_date"], past_day.isoformat())
        self.assertEqual(stages[self.item_a.id]["planned_order"], 1)


class TerminalEmployeeTasksViewQueryCountTest(TestCase):
    """_build_task_results() used to re-derive each ticket's source config,
    BOM item ids and items from scratch per ticket (several queries each) —
    the terminal's main perceived slowness. The query count must stay flat
    as the number of tickets grows, not scale with it."""

    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name="Query Count Co")
        cls.worker = Employee.objects.create(
            company=cls.company, first_name="Jose", last_name="Murillo",
            hired_at=date(2020, 1, 1), can_work_on_production=True, is_active=True,
        )
        cls.product = ProductTemplate.objects.create(
            name="Ventana", company=cls.company, production_stages=["corte"],
        )

    def _add_tickets(self, n):
        ct = ContentType.objects.get_for_model(Configuration)
        for _ in range(n):
            config = Configuration.objects.create(
                company=self.company, product=self.product, status=ConfigurationStatus.CONVERTED,
            )
            ConfigurationItem.objects.create(configuration=config, unit_price=0)
            ticket = ProductionTicket.objects.create(
                company=self.company, content_type=ct, object_id=config.id,
            )
            item = ProductionItem.objects.create(
                ticket=ticket, company=self.company,
                planning={"corte": {"status": "pending"}},
            )
            item.assigned_employees.add(self.worker)

    def test_query_count_does_not_scale_with_ticket_count(self):
        self._add_tickets(5)
        with CaptureQueriesContext(connection) as small:
            results_small = _build_task_results(self.worker, self.company)

        self._add_tickets(15)
        with CaptureQueriesContext(connection) as big:
            results_big = _build_task_results(self.worker, self.company)

        self.assertEqual(len(results_small), 5)
        self.assertEqual(len(results_big), 20)
        self.assertLessEqual(len(big.captured_queries), len(small.captured_queries) + 2)
