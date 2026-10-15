from datetime import datetime, timedelta, timezone as dt_timezone

from django.test import TestCase

from production.models import PlannedProductionSession
from production.services.session_scheduler import get_or_create_resource_for_target
from production.tests.factories import make_company, make_employee, make_item, make_machine, make_product, make_ticket

STAGE = "corte"
OTHER_STAGE = "montaje"


class SyncSessionEmployeeAssignmentTest(TestCase):
    """Scheduling an employee via the calendar (setting a session's
    primary_resource) must add them to the roster (ProductionItem.
    assigned_employees) for that ticket+stage's items — otherwise the task
    never shows up on their personal task list until someone starts it."""

    @classmethod
    def setUpTestData(cls):
        cls.company = make_company(stages=[])
        cls.product = make_product(cls.company, name="Ventana", stages=[STAGE, OTHER_STAGE])
        cls.employee = make_employee(cls.company, first_name="Jose", last_name="Murillo", stages=[])
        cls.other_employee = make_employee(cls.company, first_name="Maria", last_name="Garcia", stages=[])
        cls.machine = make_machine(
            cls.company, name="Sierra", code="M-1", stages=[], daily_capacity_minutes=480,
        )
        cls.ticket = make_ticket(cls.company, stages=[STAGE, OTHER_STAGE], product=cls.product, bom=False)
        cls.item = make_item(
            cls.ticket, stages=[STAGE], planning={STAGE: {"status": "pending"}}, production_stages_order=[],
        )
        cls.other_stage_item = make_item(
            cls.ticket, stages=[OTHER_STAGE], planning={OTHER_STAGE: {"status": "pending"}},
            production_stages_order=[],
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

    def test_deleting_a_session_without_ticket_is_a_no_op(self):
        session = PlannedProductionSession.objects.create(
            production_stage=STAGE,
            planned_start=datetime(2026, 9, 14, 8, tzinfo=dt_timezone.utc),
            planned_end=datetime(2026, 9, 14, 9, tzinfo=dt_timezone.utc),
            primary_resource=self.employee_resource,
        )
        session.delete()
        self.assertFalse(PlannedProductionSession.objects.filter(pk=session.pk).exists())
