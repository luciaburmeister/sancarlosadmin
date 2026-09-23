from datetime import date, datetime, time, timedelta

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.contrib.contenttypes.models import ContentType
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from company.models import Company, CompanyScheduleSlot, CompanyUser, Employee, WeekdayChoices
from configurator.models import Configuration, ProductTemplate
from production.models import (
    PlannedProductionSession,
    ProductionItem,
    ProductionItemSession,
    ProductionTicket,
    ProductionTicketStatus,
)
from production.services.session_scheduler import get_or_create_resource_for_target

User = get_user_model()


class ScheduleBoardApiTestBase(TestCase):
    # 2026-09-14 is a Monday.
    MONDAY = date(2026, 9, 14)

    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name="Test Co")
        cls.other_company = Company.objects.create(name="Other Co")

        CompanyScheduleSlot.objects.create(
            company=cls.company, weekday=WeekdayChoices.MONDAY,
            start_time=time(8, 0), end_time=time(17, 0),
        )

        # Shared-terminal account (kiosk login) — not an Employee.
        cls.terminal_user = User.objects.create_user(username="terminal", password="x")
        CompanyUser.objects.create(
            user=cls.terminal_user, company=cls.company, is_active=True,
            can_use_shared_terminal=True,
        )

        # Supervisor employee, granted the reschedule permission.
        cls.supervisor_user = User.objects.create_user(username="supervisor", password="x")
        cls.supervisor = Employee.objects.create(
            company=cls.company, first_name="Sup", last_name="Ervisor",
            hired_at=date(2020, 1, 1), user=cls.supervisor_user, is_active=True,
        )
        perm = Permission.objects.get(
            codename="can_reschedule_planned_sessions",
            content_type=ContentType.objects.get_for_model(PlannedProductionSession),
        )
        cls.supervisor_user.user_permissions.add(perm)

        # Same, but without the permission.
        cls.unauthorized_user = User.objects.create_user(username="nobody", password="x")
        cls.unauthorized_supervisor = Employee.objects.create(
            company=cls.company, first_name="No", last_name="Body",
            hired_at=date(2020, 1, 1), user=cls.unauthorized_user, is_active=True,
        )

        cls.worker = Employee.objects.create(
            company=cls.company, first_name="Jose", last_name="Murillo",
            hired_at=date(2020, 1, 1), can_work_on_production=True, is_active=True,
        )
        cls.resource = get_or_create_resource_for_target(cls.worker)

        cls.product = ProductTemplate.objects.create(
            name="Ventana", company=cls.company, production_stages=["corte"],
        )
        cls.config = Configuration.objects.create(
            company=cls.company, product=cls.product, notes="Vehículo con ruido en el motor",
        )
        ct = ContentType.objects.get_for_model(Configuration)
        cls.ticket = ProductionTicket.objects.create(
            company=cls.company, content_type=ct, object_id=cls.config.id,
            status=ProductionTicketStatus.IN_PROGRESS,
        )
        # Backlog/has_incomplete_stage() only look at items with real planning
        # dicts — an itemless ticket has nothing to schedule.
        ProductionItem.objects.create(
            ticket=cls.ticket, company=cls.company,
            planning={"corte": {"status": "pending"}},
        )

    def setUp(self):
        self.client = APIClient()
        self.client.force_authenticate(user=self.terminal_user)

    def _aware(self, d: date, t: time) -> datetime:
        return timezone.make_aware(datetime.combine(d, t))


class ScheduleBoardGetTest(ScheduleBoardApiTestBase):
    def test_default_week_starts_on_monday(self):
        resp = self.client.get("/es/api/production/schedule/board/")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        week_start = date.fromisoformat(body["week_start"])
        self.assertEqual(week_start.weekday(), 0)
        self.assertEqual(len(body["days"]), 7)

    def test_week_start_snaps_to_monday_of_that_week(self):
        # Wednesday of the same week should resolve to the same Monday.
        resp = self.client.get(
            "/es/api/production/schedule/board/", {"week_start": (self.MONDAY + timedelta(days=2)).isoformat()},
        )
        self.assertEqual(resp.json()["week_start"], self.MONDAY.isoformat())

    def test_session_in_range_is_returned_denormalized(self):
        session = PlannedProductionSession.objects.create(
            production_ticket=self.ticket, production_stage="corte",
            planned_start=self._aware(self.MONDAY, time(9, 0)),
            planned_end=self._aware(self.MONDAY, time(10, 0)),
            primary_resource=self.resource,
        )
        resp = self.client.get(
            "/es/api/production/schedule/board/", {"week_start": self.MONDAY.isoformat()},
        )
        body = resp.json()
        ids = [s["id"] for s in body["sessions"]]
        self.assertIn(session.id, ids)
        entry = next(s for s in body["sessions"] if s["id"] == session.id)
        self.assertEqual(entry["employee_id"], self.worker.id)
        self.assertEqual(entry["employee_name"], self.worker.full_name)
        self.assertEqual(entry["config_id"], self.config.id)
        self.assertEqual(entry["config_notes"], "Vehículo con ruido en el motor")
        self.assertEqual(entry["config_created_at"], self.config.created_at.date().isoformat())
        item = ProductionItem.objects.get(ticket=self.ticket)
        self.assertEqual(entry["production_item_ids"], [item.id])

    def test_session_stage_completed_flag_reflects_item_status(self):
        session = PlannedProductionSession.objects.create(
            production_ticket=self.ticket, production_stage="corte",
            planned_start=self._aware(self.MONDAY, time(9, 0)),
            planned_end=self._aware(self.MONDAY, time(10, 0)),
            primary_resource=self.resource,
        )
        resp = self.client.get(
            "/es/api/production/schedule/board/", {"week_start": self.MONDAY.isoformat()},
        )
        entry = next(s for s in resp.json()["sessions"] if s["id"] == session.id)
        self.assertFalse(entry["stage_completed"])

        item = ProductionItem.objects.filter(ticket=self.ticket).first()
        item.planning = {"corte": {"status": "done"}}
        item.save(update_fields=["planning"])

        resp = self.client.get(
            "/es/api/production/schedule/board/", {"week_start": self.MONDAY.isoformat()},
        )
        entry = next(s for s in resp.json()["sessions"] if s["id"] == session.id)
        self.assertTrue(entry["stage_completed"])

    def test_multi_day_session_appears_on_every_day_it_touches(self):
        wednesday = self.MONDAY + timedelta(days=2)
        session = PlannedProductionSession.objects.create(
            production_ticket=self.ticket, production_stage="corte",
            planned_start=self._aware(self.MONDAY, time(9, 0)),
            planned_end=self._aware(wednesday, time(10, 0)),
            primary_resource=self.resource,
        )
        resp = self.client.get(
            "/es/api/production/schedule/board/", {"week_start": self.MONDAY.isoformat()},
        )
        entry = next(s for s in resp.json()["sessions"] if s["id"] == session.id)
        self.assertEqual(date.fromisoformat(entry["planned_start"][:10]), self.MONDAY)
        self.assertEqual(date.fromisoformat(entry["planned_end"][:10]), wednesday)

    def test_backlog_entry_includes_production_item_ids(self):
        resp = self.client.get(
            "/es/api/production/schedule/board/", {"week_start": self.MONDAY.isoformat()},
        )
        entry = next(b for b in resp.json()["backlog"] if b["ticket_id"] == self.ticket.id)
        item = ProductionItem.objects.get(ticket=self.ticket)
        self.assertEqual(entry["production_item_ids"], [item.id])

    def test_session_from_other_company_is_not_visible(self):
        other_worker = Employee.objects.create(
            company=self.other_company, first_name="Other", last_name="Co",
            hired_at=date(2020, 1, 1), can_work_on_production=True, is_active=True,
        )
        other_resource = get_or_create_resource_for_target(other_worker)
        PlannedProductionSession.objects.create(
            production_stage="corte",
            planned_start=self._aware(self.MONDAY, time(9, 0)),
            planned_end=self._aware(self.MONDAY, time(10, 0)),
            primary_resource=other_resource,
        )
        resp = self.client.get(
            "/es/api/production/schedule/board/", {"week_start": self.MONDAY.isoformat()},
        )
        employee_ids = {e["id"] for e in resp.json()["employees"]}
        self.assertNotIn(other_worker.id, employee_ids)

    def test_unscheduled_backlog_entry_has_is_scheduled_false(self):
        resp = self.client.get(
            "/es/api/production/schedule/board/", {"week_start": self.MONDAY.isoformat()},
        )
        entry = next(b for b in resp.json()["backlog"] if b["ticket_id"] == self.ticket.id)
        self.assertFalse(entry["is_scheduled"])

    def _multi_stage_ticket(self):
        """A second, independent ticket with two stages — for is_scheduled
        coverage, kept separate from the shared single-stage fixture ticket."""
        product = ProductTemplate.objects.create(
            name="Puerta", company=self.company, production_stages=["corte", "pintura"],
        )
        config = Configuration.objects.create(company=self.company, product=product)
        ct = ContentType.objects.get_for_model(Configuration)
        ticket = ProductionTicket.objects.create(
            company=self.company, content_type=ct, object_id=config.id,
            status=ProductionTicketStatus.IN_PROGRESS,
        )
        ProductionItem.objects.create(
            ticket=ticket, company=self.company,
            planning={"corte": {"status": "pending"}, "pintura": {"status": "pending"}},
        )
        return ticket

    def test_mixed_ticket_shows_both_scheduled_and_unscheduled_stages(self):
        """One stage already planned, the other not — the group should show
        both (grouped by ticket_id on the client), flagged accordingly,
        instead of the planned one just disappearing."""
        ticket = self._multi_stage_ticket()
        PlannedProductionSession.objects.create(
            production_ticket=ticket, production_stage="corte",
            planned_start=self._aware(self.MONDAY, time(9, 0)),
            planned_end=self._aware(self.MONDAY, time(10, 0)),
            primary_resource=self.resource,
        )
        resp = self.client.get(
            "/es/api/production/schedule/board/", {"week_start": self.MONDAY.isoformat()},
        )
        entries = {b["production_stage"]: b for b in resp.json()["backlog"] if b["ticket_id"] == ticket.id}
        self.assertEqual(set(entries), {"corte", "pintura"})
        self.assertTrue(entries["corte"]["is_scheduled"])
        self.assertFalse(entries["pintura"]["is_scheduled"])

    def test_fully_scheduled_ticket_excluded_from_backlog_but_kept_as_row(self):
        ticket = self._multi_stage_ticket()
        for stage in ("corte", "pintura"):
            PlannedProductionSession.objects.create(
                production_ticket=ticket, production_stage=stage,
                planned_start=self._aware(self.MONDAY, time(9, 0)),
                planned_end=self._aware(self.MONDAY, time(10, 0)),
                primary_resource=self.resource,
            )
        resp = self.client.get(
            "/es/api/production/schedule/board/", {"week_start": self.MONDAY.isoformat()},
        )
        body = resp.json()
        self.assertNotIn(ticket.id, {b["ticket_id"] for b in body["backlog"]})
        self.assertIn(ticket.id, {c["ticket_id"] for c in body["unfinished_configurations"]})

    def test_active_work_appears_in_activity(self):
        # Turn the fixture item's stage into "currently being worked" and
        # start an open session on it, within the displayed week.
        item = ProductionItem.objects.filter(ticket=self.ticket).first()
        item.planning = {"corte": {"status": "in_progress"}}
        item.save(update_fields=["planning"])
        ProductionItemSession.objects.create(
            production_item=item, company=self.company, production_stage="corte",
            status="in_progress", start_time=self._aware(self.MONDAY, time(9, 0)),
            created_by=self.worker,
        )

        resp = self.client.get(
            "/es/api/production/schedule/board/", {"week_start": self.MONDAY.isoformat()},
        )
        activity = resp.json()["activity"]
        self.assertEqual(len(activity), 1)
        self.assertEqual(activity[0]["kind"], "active")
        self.assertEqual(activity[0]["production_ticket_id"], self.ticket.id)
        self.assertEqual(activity[0]["employee_id"], self.worker.id)
        self.assertEqual(activity[0]["config_created_at"], self.config.created_at.date().isoformat())

    def test_in_progress_stage_is_excluded_from_backlog(self):
        """A stage already being worked (active/paused) shouldn't also show
        up in the backlog as "needs planning" — it's already handled, and
        shows in `activity` instead. The row should still appear though,
        since there's still real, unfinished work to watch."""
        item = ProductionItem.objects.filter(ticket=self.ticket).first()
        item.planning = {"corte": {"status": "in_progress"}}
        item.save(update_fields=["planning"])

        resp = self.client.get(
            "/es/api/production/schedule/board/", {"week_start": self.MONDAY.isoformat()},
        )
        body = resp.json()
        self.assertNotIn(self.ticket.id, {b["ticket_id"] for b in body["backlog"]})
        self.assertIn(self.config.id, {c["config_id"] for c in body["unfinished_configurations"]})

    def test_unfinished_ticket_without_planned_session_appears_in_backlog(self):
        resp = self.client.get(
            "/es/api/production/schedule/board/", {"week_start": self.MONDAY.isoformat()},
        )
        body = resp.json()
        backlog_tickets = {b["ticket_id"] for b in body["backlog"]}
        self.assertIn(self.ticket.id, backlog_tickets)
        config_ids = {c["config_id"] for c in body["unfinished_configurations"]}
        self.assertIn(self.config.id, config_ids)

    def test_ticket_with_stale_status_but_no_incomplete_stage_is_excluded(self):
        """ticket.status can lag behind reality (only recomputed on certain
        events) — a ticket whose only item's stage is actually 'done' must
        not show as a row/backlog entry just because status is still
        'pending', or a finished configuration keeps cluttering the board."""
        ct = ContentType.objects.get_for_model(Configuration)
        stale_config = Configuration.objects.create(company=self.company, product=self.product)
        stale_ticket = ProductionTicket.objects.create(
            company=self.company, content_type=ct, object_id=stale_config.id,
            status=ProductionTicketStatus.PENDING,
        )
        ProductionItem.objects.create(
            ticket=stale_ticket, company=self.company, planning={"corte": {"status": "done"}},
        )

        resp = self.client.get(
            "/es/api/production/schedule/board/", {"week_start": self.MONDAY.isoformat()},
        )
        body = resp.json()
        self.assertNotIn(stale_ticket.id, {c["ticket_id"] for c in body["unfinished_configurations"]})
        self.assertNotIn(stale_ticket.id, {b["ticket_id"] for b in body["backlog"]})


class RescheduleSessionApiTest(ScheduleBoardApiTestBase):
    def setUp(self):
        super().setUp()
        self.session = PlannedProductionSession.objects.create(
            production_ticket=self.ticket, production_stage="corte",
            planned_start=self._aware(self.MONDAY, time(9, 0)),
            planned_end=self._aware(self.MONDAY, time(10, 0)),
            primary_resource=self.resource,
        )

    def _url(self, session_id):
        return f"/es/api/production/schedule/sessions/{session_id}/reschedule/"

    def test_without_permission_is_rejected(self):
        resp = self.client.post(
            self._url(self.session.id),
            {"supervisor_id": self.unauthorized_supervisor.id, "date": self.MONDAY.isoformat()},
            format="json",
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("supervisor_id", resp.json()["details"])

    def test_auto_snap_moves_session(self):
        tuesday = self.MONDAY + timedelta(days=1)
        CompanyScheduleSlot.objects.create(
            company=self.company, weekday=WeekdayChoices.TUESDAY,
            start_time=time(8, 0), end_time=time(17, 0),
        )
        resp = self.client.post(
            self._url(self.session.id),
            {"supervisor_id": self.supervisor.id, "date": tuesday.isoformat()},
            format="json",
        )
        self.assertEqual(resp.status_code, 200)
        self.session.refresh_from_db()
        self.assertEqual(timezone.localtime(self.session.planned_start).date(), tuesday)

    def test_explicit_time_conflict_returns_409(self):
        PlannedProductionSession.objects.create(
            production_stage="pintura",
            planned_start=self._aware(self.MONDAY, time(11, 0)),
            planned_end=self._aware(self.MONDAY, time(12, 0)),
            primary_resource=self.resource,
        )
        resp = self.client.post(
            self._url(self.session.id),
            {
                "supervisor_id": self.supervisor.id, "date": self.MONDAY.isoformat(),
                "start_time": "11:30", "end_time": "12:15",
            },
            format="json",
        )
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.json()["code"], "planned_session_conflict")

    def test_end_date_moves_session_end_to_a_later_day(self):
        wednesday = self.MONDAY + timedelta(days=2)
        CompanyScheduleSlot.objects.create(
            company=self.company, weekday=WeekdayChoices.WEDNESDAY,
            start_time=time(8, 0), end_time=time(17, 0),
        )
        resp = self.client.post(
            self._url(self.session.id),
            {
                "supervisor_id": self.supervisor.id, "date": self.MONDAY.isoformat(),
                "start_time": "09:00", "end_time": "10:00", "end_date": wednesday.isoformat(),
            },
            format="json",
        )
        self.assertEqual(resp.status_code, 200)
        self.session.refresh_from_db()
        self.assertEqual(timezone.localtime(self.session.planned_start).date(), self.MONDAY)
        self.assertEqual(timezone.localtime(self.session.planned_end).date(), wednesday)

    def test_explicit_time_outside_working_hours_returns_400(self):
        resp = self.client.post(
            self._url(self.session.id),
            {
                "supervisor_id": self.supervisor.id, "date": self.MONDAY.isoformat(),
                "start_time": "18:00", "end_time": "19:00",
            },
            format="json",
        )
        self.assertEqual(resp.status_code, 400)

    def test_locked_session_returns_400(self):
        self.session.locked = True
        self.session.save(update_fields=["locked"])
        resp = self.client.post(
            self._url(self.session.id),
            {"supervisor_id": self.supervisor.id, "date": self.MONDAY.isoformat()},
            format="json",
        )
        self.assertEqual(resp.status_code, 400)

    def test_session_from_other_company_returns_404(self):
        other_worker = Employee.objects.create(
            company=self.other_company, first_name="Other", last_name="Co",
            hired_at=date(2020, 1, 1), can_work_on_production=True, is_active=True,
        )
        other_resource = get_or_create_resource_for_target(other_worker)
        other_session = PlannedProductionSession.objects.create(
            production_stage="corte",
            planned_start=self._aware(self.MONDAY, time(9, 0)),
            planned_end=self._aware(self.MONDAY, time(10, 0)),
            primary_resource=other_resource,
        )
        resp = self.client.post(
            self._url(other_session.id),
            {"supervisor_id": self.supervisor.id, "date": self.MONDAY.isoformat()},
            format="json",
        )
        self.assertEqual(resp.status_code, 404)


class DeleteSessionApiTest(ScheduleBoardApiTestBase):
    def setUp(self):
        super().setUp()
        self.session = PlannedProductionSession.objects.create(
            production_ticket=self.ticket, production_stage="corte",
            planned_start=self._aware(self.MONDAY, time(9, 0)),
            planned_end=self._aware(self.MONDAY, time(10, 0)),
            primary_resource=self.resource,
        )

    def _url(self, session_id):
        return f"/es/api/production/schedule/sessions/{session_id}/delete/"

    def test_without_permission_is_rejected(self):
        resp = self.client.post(
            self._url(self.session.id),
            {"supervisor_id": self.unauthorized_supervisor.id},
            format="json",
        )
        self.assertEqual(resp.status_code, 400)
        self.assertTrue(PlannedProductionSession.objects.filter(id=self.session.id).exists())

    def test_unassign_deletes_session_and_returns_to_backlog(self):
        resp = self.client.post(
            self._url(self.session.id),
            {"supervisor_id": self.supervisor.id},
            format="json",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(PlannedProductionSession.objects.filter(id=self.session.id).exists())

        board = self.client.get(
            "/es/api/production/schedule/board/", {"week_start": self.MONDAY.isoformat()},
        ).json()
        entry = next(b for b in board["backlog"] if b["ticket_id"] == self.ticket.id)
        self.assertFalse(entry["is_scheduled"])

    def test_locked_session_returns_400(self):
        self.session.locked = True
        self.session.save(update_fields=["locked"])
        resp = self.client.post(
            self._url(self.session.id),
            {"supervisor_id": self.supervisor.id},
            format="json",
        )
        self.assertEqual(resp.status_code, 400)
        self.assertTrue(PlannedProductionSession.objects.filter(id=self.session.id).exists())

    def test_session_from_other_company_returns_404(self):
        other_worker = Employee.objects.create(
            company=self.other_company, first_name="Other", last_name="Co",
            hired_at=date(2020, 1, 1), can_work_on_production=True, is_active=True,
        )
        other_resource = get_or_create_resource_for_target(other_worker)
        other_session = PlannedProductionSession.objects.create(
            production_stage="corte",
            planned_start=self._aware(self.MONDAY, time(9, 0)),
            planned_end=self._aware(self.MONDAY, time(10, 0)),
            primary_resource=other_resource,
        )
        resp = self.client.post(
            self._url(other_session.id),
            {"supervisor_id": self.supervisor.id},
            format="json",
        )
        self.assertEqual(resp.status_code, 404)


class CreateSessionFromBacklogApiTest(ScheduleBoardApiTestBase):
    URL = "/es/api/production/schedule/sessions/create/"

    def test_creates_session_from_backlog(self):
        resp = self.client.post(
            self.URL,
            {
                "supervisor_id": self.supervisor.id,
                "production_ticket_id": self.ticket.id,
                "production_stage": "corte",
                "employee_id": self.worker.id,
                "date": self.MONDAY.isoformat(),
                "start_time": "09:00",
                "end_time": "10:00",
            },
            format="json",
        )
        self.assertEqual(resp.status_code, 201)
        session_id = resp.json()["session"]["id"]
        session = PlannedProductionSession.objects.get(id=session_id)
        self.assertEqual(session.source, "manual")
        self.assertEqual(session.primary_resource_id, self.resource.id)

    def test_creates_session_without_time_auto_snaps(self):
        resp = self.client.post(
            self.URL,
            {
                "supervisor_id": self.supervisor.id,
                "production_ticket_id": self.ticket.id,
                "production_stage": "corte",
                "employee_id": self.worker.id,
                "date": self.MONDAY.isoformat(),
            },
            format="json",
        )
        self.assertEqual(resp.status_code, 201)
        session = PlannedProductionSession.objects.get(id=resp.json()["session"]["id"])
        self.assertEqual(timezone.localtime(session.planned_start).time(), time(8, 0))

    def test_conflict_returns_409(self):
        PlannedProductionSession.objects.create(
            production_stage="corte",
            planned_start=self._aware(self.MONDAY, time(9, 0)),
            planned_end=self._aware(self.MONDAY, time(10, 0)),
            primary_resource=self.resource,
        )
        resp = self.client.post(
            self.URL,
            {
                "supervisor_id": self.supervisor.id,
                "production_ticket_id": self.ticket.id,
                "production_stage": "corte",
                "employee_id": self.worker.id,
                "date": self.MONDAY.isoformat(),
                "start_time": "09:30",
                "end_time": "10:30",
            },
            format="json",
        )
        self.assertEqual(resp.status_code, 409)

    def test_creates_multi_day_session_with_end_date(self):
        wednesday = self.MONDAY + timedelta(days=2)
        CompanyScheduleSlot.objects.create(
            company=self.company, weekday=WeekdayChoices.WEDNESDAY,
            start_time=time(8, 0), end_time=time(17, 0),
        )
        resp = self.client.post(
            self.URL,
            {
                "supervisor_id": self.supervisor.id,
                "production_ticket_id": self.ticket.id,
                "production_stage": "corte",
                "employee_id": self.worker.id,
                "date": self.MONDAY.isoformat(),
                "start_time": "09:00",
                "end_time": "10:00",
                "end_date": wednesday.isoformat(),
            },
            format="json",
        )
        self.assertEqual(resp.status_code, 201)
        session = PlannedProductionSession.objects.get(id=resp.json()["session"]["id"])
        self.assertEqual(timezone.localtime(session.planned_start).date(), self.MONDAY)
        self.assertEqual(timezone.localtime(session.planned_end).date(), wednesday)


class ReorderDaySessionsApiTest(ScheduleBoardApiTestBase):
    URL = "/es/api/production/schedule/sessions/reorder-day/"

    def setUp(self):
        super().setUp()
        self.first = PlannedProductionSession.objects.create(
            production_stage="corte",
            planned_start=self._aware(self.MONDAY, time(8, 0)),
            planned_end=self._aware(self.MONDAY, time(9, 0)),
            primary_resource=self.resource,
        )
        self.second = PlannedProductionSession.objects.create(
            production_stage="pintura",
            planned_start=self._aware(self.MONDAY, time(9, 0)),
            planned_end=self._aware(self.MONDAY, time(9, 30)),
            primary_resource=self.resource,
        )

    def test_reorders_and_repacks_times(self):
        resp = self.client.post(
            self.URL,
            {
                "supervisor_id": self.supervisor.id,
                "employee_id": self.worker.id,
                "date": self.MONDAY.isoformat(),
                "session_ids": [self.second.id, self.first.id],
            },
            format="json",
        )
        self.assertEqual(resp.status_code, 200)
        self.first.refresh_from_db()
        self.second.refresh_from_db()
        self.assertEqual(timezone.localtime(self.second.planned_start).time(), time(8, 0))
        self.assertEqual(timezone.localtime(self.first.planned_start).time(), time(8, 30))

    def test_without_permission_is_rejected(self):
        resp = self.client.post(
            self.URL,
            {
                "supervisor_id": self.unauthorized_supervisor.id,
                "employee_id": self.worker.id,
                "date": self.MONDAY.isoformat(),
                "session_ids": [self.first.id, self.second.id],
            },
            format="json",
        )
        self.assertEqual(resp.status_code, 400)

    def test_incomplete_set_returns_400(self):
        resp = self.client.post(
            self.URL,
            {
                "supervisor_id": self.supervisor.id,
                "employee_id": self.worker.id,
                "date": self.MONDAY.isoformat(),
                "session_ids": [self.first.id],
            },
            format="json",
        )
        self.assertEqual(resp.status_code, 400)
