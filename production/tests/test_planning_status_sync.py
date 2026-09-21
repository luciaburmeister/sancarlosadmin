from datetime import date, datetime, timedelta, timezone as dt_timezone

from django.contrib.contenttypes.models import ContentType
from django.test import TestCase

from company.models import Company, Employee, Machine
from configurator.models import Configuration, ProductTemplate
from production.models import PlannedProductionSession, ProductionItem, ProductionTicket
from production.services.session_scheduler import get_or_create_resource_for_target

STAGE = "corte"
OTHER_STAGE = "montaje"


class SyncSessionEmployeeAssignmentTest(TestCase):
    """Scheduling an employee via the calendar (setting a session's
    primary_resource) must add them to the roster (ProductionItem.
    assigned_employees) for that ticket+stage's items — otherwise the task
    never shows up on their personal task list until someone starts it."""

    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name="Test Co")
        cls.product = ProductTemplate.objects.create(
            name="Ventana", company=cls.company, production_stages=[STAGE, OTHER_STAGE],
        )
        cls.employee = Employee.objects.create(
            company=cls.company, first_name="Jose", last_name="Murillo",
            hired_at=date(2020, 1, 1), can_work_on_production=True,
        )
        cls.other_employee = Employee.objects.create(
            company=cls.company, first_name="Maria", last_name="Garcia",
            hired_at=date(2020, 1, 1), can_work_on_production=True,
        )
        cls.machine = Machine.objects.create(
            company=cls.company, name="Sierra", code="M-1", daily_capacity_minutes=480,
        )
        cls.config = Configuration.objects.create(company=cls.company, product=cls.product)
        ct = ContentType.objects.get_for_model(Configuration)
        cls.ticket = ProductionTicket.objects.create(
            company=cls.company, content_type=ct, object_id=cls.config.id,
        )
        cls.item = ProductionItem.objects.create(
            ticket=cls.ticket, company=cls.company,
            planning={STAGE: {"status": "pending"}},
        )
        cls.other_stage_item = ProductionItem.objects.create(
            ticket=cls.ticket, company=cls.company,
            planning={OTHER_STAGE: {"status": "pending"}},
        )
        cls.employee_resource = get_or_create_resource_for_target(cls.employee)
        cls.other_employee_resource = get_or_create_resource_for_target(cls.other_employee)
        cls.machine_resource = get_or_create_resource_for_target(cls.machine)

    def _session(self, resource, stage=STAGE, start_offset=0):
        start = datetime(2026, 9, 14, 8, tzinfo=dt_timezone.utc) + timedelta(days=start_offset)
        return PlannedProductionSession.objects.create(
            production_ticket=self.ticket, production_stage=stage,
            planned_start=start, planned_end=start + timedelta(hours=1),
            primary_resource=resource,
        )

    def test_scheduling_employee_adds_them_to_stage_items_roster(self):
        self._session(self.employee_resource)
        self.assertIn(self.employee, self.item.assigned_employees.all())

    def test_does_not_touch_items_of_a_different_stage(self):
        self._session(self.employee_resource)
        self.assertNotIn(self.employee, self.other_stage_item.assigned_employees.all())

    def test_reassigning_only_adds_the_new_employee_never_removes_the_old_one(self):
        session = self._session(self.employee_resource)
        session.primary_resource = self.other_employee_resource
        session.save()

        assigned = set(self.item.assigned_employees.all())
        self.assertIn(self.employee, assigned)
        self.assertIn(self.other_employee, assigned)

    def test_machine_resource_is_ignored(self):
        self._session(self.machine_resource)
        self.assertEqual(self.item.assigned_employees.count(), 0)

    def test_session_without_ticket_is_a_no_op(self):
        # A session can in principle be saved without a ticket link (e.g.
        # machine-only maintenance blocks) — must not raise.
        PlannedProductionSession.objects.create(
            production_stage=STAGE,
            planned_start=datetime(2026, 9, 14, 8, tzinfo=dt_timezone.utc),
            planned_end=datetime(2026, 9, 14, 9, tzinfo=dt_timezone.utc),
            primary_resource=self.employee_resource,
        )
        # No exception raised is the assertion here.
