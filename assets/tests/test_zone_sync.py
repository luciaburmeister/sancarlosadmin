"""
assets/tests/test_zone_sync.py

Tests for automatic current_working_zone sync (Asset + Employee) driven by:
  - AssetTransaction._sync_zones_on_porte_delivery(): fires on creation of a
    PORTE_DELIVERY transaction linked to an Own Machine Transport planning.
  - AssetPlanningRequest.apply_zone_change() / the planning_type == NONE
    approve flow (Machinist Movement, Machine Self-Transport — no
    AssetPlanning involved).
  - AssetPlanningRequest._sync_zone_sync_schedule(): schedules a one-off
    django_celery_beat task for the exact needed_date/needed_time on
    approval (no polling), and cancels it if the decision is reverted.

See assets/models.py, assets/admin/asset_planning_request.py, assets/tasks.py.

Run just this file:
    python manage.py test assets.tests.test_zone_sync
"""
import json
from datetime import date, datetime, time, timedelta

from django.contrib.auth import get_user_model
from django.contrib.messages.middleware import MessageMiddleware
from django.contrib.sessions.middleware import SessionMiddleware
from django.test import RequestFactory, TestCase
from django.utils import timezone
from django_celery_beat.models import PeriodicTask

from assets.admin.asset_planning_request import AssetPlanningRequestAdmin
from assets.constants import (
    SERVICE_TYPE_MAQUINA_PROPIA, SERVICE_TYPE_MAQUINISTAS, SERVICE_TYPE_MATERIAL_EXTERNO,
    SERVICE_TYPE_AUTOPORTE_MAQUINA, SERVICE_TYPE_PEONES,
)
from assets.models import AssetPlanningRequest, AssetPlanningRequestStatus, AssetTransaction, PlanningType, TransactionTypeChoices
from assets.tasks import apply_scheduled_zone_change
from management.models import ProjectFile
from sales.models import Client
from api.fleet.tests.helpers import make_asset, make_asset_type, make_company, make_employee, make_planning, make_user, make_zone

User = get_user_model()


class PorteDeliveryZoneSyncTests(TestCase):
    def setUp(self):
        self.company = make_company()
        self.origin = make_zone(self.company, name="Origin")
        self.destination = make_zone(self.company, name="Destination")
        self.asset_type = make_asset_type(self.company, name="Machine")
        self.machine = make_asset(self.company, self.asset_type, zone=self.origin, name="Machine 1")
        self.driver = make_employee(self.company, make_user(username="driver"))
        self.operator = make_employee(self.company, make_user(username="operator"))

    def _make_planning(self, **overrides):
        overrides.setdefault("service_type", SERVICE_TYPE_MAQUINA_PROPIA)
        overrides.setdefault("planning_type", "porte")
        overrides.setdefault("origin", self.origin)
        overrides.setdefault("destination", self.destination)
        # asset is the cuba the machine travels in (irrelevant to zone-sync);
        # machine is what AssetPlanning.machine / _sync_zones_on_porte_delivery
        # actually tracks — see assets/models.py.
        overrides.setdefault("machine", self.machine)
        return make_planning(self.company, asset=self.machine, assigned_to=self.driver, **overrides)

    def test_delivery_updates_machine_zone(self):
        planning = self._make_planning()
        AssetTransaction.objects.create(
            company=self.company, asset=self.machine, planning=planning,
            from_zone=self.origin, to_zone=self.destination,
            transaction_type=TransactionTypeChoices.PORTE_DELIVERY,
        )
        self.machine.refresh_from_db()
        self.assertEqual(self.machine.current_working_zone_id, self.destination.id)

    def test_delivery_updates_machine_zone_even_without_machinist(self):
        planning = self._make_planning(machinist_moved=False)
        AssetTransaction.objects.create(
            company=self.company, asset=self.machine, planning=planning,
            from_zone=self.origin, to_zone=self.destination,
            transaction_type=TransactionTypeChoices.PORTE_DELIVERY,
        )
        self.machine.refresh_from_db()
        self.assertEqual(self.machine.current_working_zone_id, self.destination.id)
        self.operator.refresh_from_db()
        self.assertIsNone(self.operator.current_working_zone_id)

    def test_delivery_moves_machinists_when_machinist_moved(self):
        planning = self._make_planning(machinist_moved=True)
        planning.moved_employees.set([self.operator])
        AssetTransaction.objects.create(
            company=self.company, asset=self.machine, planning=planning,
            from_zone=self.origin, to_zone=self.destination,
            transaction_type=TransactionTypeChoices.PORTE_DELIVERY,
        )
        self.operator.refresh_from_db()
        self.assertEqual(self.operator.current_working_zone_id, self.destination.id)
        self.assertIsNotNone(self.operator.zone_changed_at)

    def test_pickup_does_not_move_zone(self):
        """A PORTE_MOVEMENT (recogida) transaction must not update zones — only entrega does."""
        planning = self._make_planning(machinist_moved=True)
        planning.moved_employees.set([self.operator])
        AssetTransaction.objects.create(
            company=self.company, asset=self.machine, planning=planning,
            from_zone=self.origin, to_zone=self.destination,
            transaction_type=TransactionTypeChoices.PORTE_MOVEMENT,
        )
        self.machine.refresh_from_db()
        self.assertEqual(self.machine.current_working_zone_id, self.origin.id)

    def test_delivery_for_other_service_type_does_not_sync(self):
        """The fix is scoped to Own Machine Transport — other porte service types are unaffected."""
        planning = self._make_planning(service_type=SERVICE_TYPE_MATERIAL_EXTERNO)
        AssetTransaction.objects.create(
            company=self.company, asset=self.machine, planning=planning,
            from_zone=self.origin, to_zone=self.destination,
            transaction_type=TransactionTypeChoices.PORTE_DELIVERY,
        )
        self.machine.refresh_from_db()
        self.assertEqual(self.machine.current_working_zone_id, self.origin.id)

    def test_editing_an_existing_delivery_does_not_resync(self):
        """The sync only runs on creation — later edits (e.g. notes) must not re-trigger it."""
        planning = self._make_planning()
        tx = AssetTransaction.objects.create(
            company=self.company, asset=self.machine, planning=planning,
            from_zone=self.origin, to_zone=self.destination,
            transaction_type=TransactionTypeChoices.PORTE_DELIVERY,
        )
        self.machine.current_working_zone = self.origin
        self.machine.save()
        tx.notes = "edited"
        tx.save()
        self.machine.refresh_from_db()
        self.assertEqual(self.machine.current_working_zone_id, self.origin.id)


class MachinistMovementRequestTests(TestCase):
    def setUp(self):
        self.company = make_company()
        self.from_zone = make_zone(self.company, name="From Zone")
        self.to_zone = make_zone(self.company, name="To Zone")
        client = Client.objects.create(name="Client 1", company=self.company)
        self.project_file = ProjectFile.objects.create(name="Project 1", client=client, company=self.company)
        self.machinist = make_employee(self.company, make_user(username="machinist1"))

        self.superuser = User.objects.create_superuser(username="admin", password="testpass123", email="a@a.com")
        make_employee(self.company, self.superuser)

    def _make_request(self, needed_date):
        req = AssetPlanningRequest(
            company=self.company,
            service_type=SERVICE_TYPE_MAQUINISTAS,
            to_zone=self.to_zone,
            from_zone=self.from_zone,
            needed_date=needed_date,
            project_file=self.project_file,
        )
        req.full_clean(exclude=["planning_type"])
        req.save()
        req.moved_employees.set([self.machinist])
        return req

    def _approve(self, req):
        request = RequestFactory().post("/")
        SessionMiddleware(lambda r: None).process_request(request)
        request.session.save()
        MessageMiddleware(lambda r: None).process_request(request)
        request.user = self.superuser
        AssetPlanningRequestAdmin(AssetPlanningRequest, admin_site=None).approve_submit_action(request, req)

    def test_clean_does_not_require_estimated_duration(self):
        req = self._make_request(date.today())
        self.assertIsNone(req.estimated_duration)
        self.assertEqual(req.planning_type, PlanningType.NONE)

    def test_approve_today_moves_machinist_immediately_without_planning(self):
        req = self._make_request(date.today())
        self._approve(req)
        req.refresh_from_db()
        self.assertEqual(req.status, AssetPlanningRequestStatus.APPROVED)
        self.assertIsNone(req.resulting_planning)
        self.assertIsNotNone(req.zone_sync_applied_at)
        self.machinist.refresh_from_db()
        self.assertEqual(self.machinist.current_working_zone_id, self.to_zone.id)

    def test_approve_future_date_does_not_move_machinist_yet(self):
        req = self._make_request(date.today() + timedelta(days=3))
        self._approve(req)
        req.refresh_from_db()
        self.assertEqual(req.status, AssetPlanningRequestStatus.APPROVED)
        self.assertIsNone(req.zone_sync_applied_at)
        self.machinist.refresh_from_db()
        self.assertIsNone(self.machinist.current_working_zone_id)

    def test_approve_without_machinists_selected_is_rejected(self):
        req = self._make_request(date.today())
        req.moved_employees.clear()
        self._approve(req)
        req.refresh_from_db()
        self.assertEqual(req.status, AssetPlanningRequestStatus.PENDING_REVIEW)


class LaborerMovementRequestTests(TestCase):
    """Same mechanism as MachinistMovementRequestTests, but for SERVICE_TYPE_PEONES."""

    def setUp(self):
        self.company = make_company()
        self.from_zone = make_zone(self.company, name="From Zone")
        self.to_zone = make_zone(self.company, name="To Zone")
        self.laborer = make_employee(self.company, make_user(username="laborer1"))

        self.superuser = User.objects.create_superuser(username="admin2", password="testpass123", email="b@a.com")
        make_employee(self.company, self.superuser)

    def _make_request(self, needed_date):
        req = AssetPlanningRequest(
            company=self.company,
            service_type=SERVICE_TYPE_PEONES,
            to_zone=self.to_zone,
            from_zone=self.from_zone,
            needed_date=needed_date,
        )
        # No project_file — no longer required for planning_type == NONE.
        req.full_clean(exclude=["planning_type"])
        req.save()
        req.moved_employees.set([self.laborer])
        return req

    def _approve(self, req):
        request = RequestFactory().post("/")
        SessionMiddleware(lambda r: None).process_request(request)
        request.session.save()
        MessageMiddleware(lambda r: None).process_request(request)
        request.user = self.superuser
        AssetPlanningRequestAdmin(AssetPlanningRequest, admin_site=None).approve_submit_action(request, req)

    def test_clean_does_not_require_project_file(self):
        req = self._make_request(date.today())
        self.assertIsNone(req.project_file_id)
        self.assertEqual(req.planning_type, PlanningType.NONE)

    def test_approve_today_moves_laborer_immediately_without_planning(self):
        req = self._make_request(date.today())
        self._approve(req)
        req.refresh_from_db()
        self.assertEqual(req.status, AssetPlanningRequestStatus.APPROVED)
        self.assertIsNone(req.resulting_planning)
        self.assertIsNotNone(req.zone_sync_applied_at)
        self.laborer.refresh_from_db()
        self.assertEqual(self.laborer.current_working_zone_id, self.to_zone.id)

    def test_approve_future_date_does_not_move_laborer_yet(self):
        req = self._make_request(date.today() + timedelta(days=3))
        self._approve(req)
        req.refresh_from_db()
        self.assertIsNone(req.zone_sync_applied_at)
        self.laborer.refresh_from_db()
        self.assertIsNone(self.laborer.current_working_zone_id)


class AutoporteMaquinaRequestTests(TestCase):
    """Machine Self-Transport: no truck, no AssetPlanning — just moves the
    machine (and optionally machinists riding along) to to_zone."""

    def setUp(self):
        self.company = make_company()
        self.from_zone = make_zone(self.company, name="From Zone")
        self.to_zone = make_zone(self.company, name="To Zone")
        client = Client.objects.create(name="Client 1", company=self.company)
        self.project_file = ProjectFile.objects.create(name="Project 1", client=client, company=self.company)
        self.asset_type = make_asset_type(self.company, name="Machine")
        self.machine = make_asset(self.company, self.asset_type, zone=self.from_zone, name="Machine 1")
        self.machinist = make_employee(self.company, make_user(username="autoporte_machinist"))

        self.superuser = User.objects.create_superuser(username="admin_autoporte", password="testpass123", email="a3@a.com")
        make_employee(self.company, self.superuser)

    def _make_request(self, needed_date, needed_time=None, with_machinist=False):
        req = AssetPlanningRequest(
            company=self.company,
            service_type=SERVICE_TYPE_AUTOPORTE_MAQUINA,
            to_zone=self.to_zone,
            asset=self.machine,
            needed_date=needed_date,
            needed_time=needed_time,
            project_file=self.project_file,
        )
        req.full_clean(exclude=["planning_type"])
        req.save()
        if with_machinist:
            req.moved_employees.set([self.machinist])
        return req

    def _approve(self, req):
        request = RequestFactory().post("/")
        SessionMiddleware(lambda r: None).process_request(request)
        request.session.save()
        MessageMiddleware(lambda r: None).process_request(request)
        request.user = self.superuser
        AssetPlanningRequestAdmin(AssetPlanningRequest, admin_site=None).approve_submit_action(request, req)

    def test_clean_does_not_require_from_zone_or_estimated_duration(self):
        req = self._make_request(date.today())
        self.assertIsNone(req.from_zone_id)
        self.assertIsNone(req.estimated_duration)
        self.assertEqual(req.planning_type, PlanningType.NONE)

    def test_approve_moves_machine_alone_without_machinists(self):
        req = self._make_request(date.today())
        self._approve(req)
        req.refresh_from_db()
        self.assertEqual(req.status, AssetPlanningRequestStatus.APPROVED)
        self.assertIsNone(req.resulting_planning)
        self.machine.refresh_from_db()
        self.assertEqual(self.machine.current_working_zone_id, self.to_zone.id)

    def test_approve_moves_machine_and_machinist_together(self):
        req = self._make_request(date.today(), with_machinist=True)
        self._approve(req)
        self.machine.refresh_from_db()
        self.machinist.refresh_from_db()
        self.assertEqual(self.machine.current_working_zone_id, self.to_zone.id)
        self.assertEqual(self.machinist.current_working_zone_id, self.to_zone.id)

    def test_approve_future_date_does_not_move_yet(self):
        req = self._make_request(date.today() + timedelta(days=2))
        self._approve(req)
        req.refresh_from_db()
        self.assertIsNone(req.zone_sync_applied_at)
        self.machine.refresh_from_db()
        self.assertEqual(self.machine.current_working_zone_id, self.from_zone.id)

    def test_approve_today_with_future_needed_time_does_not_move_yet(self):
        req = self._make_request(date.today(), needed_time=time(23, 59, 59))
        self._approve(req)
        req.refresh_from_db()
        self.assertIsNone(req.zone_sync_applied_at)
        self.machine.refresh_from_db()
        self.assertEqual(self.machine.current_working_zone_id, self.from_zone.id)

    def test_approve_today_with_past_needed_time_moves_immediately(self):
        req = self._make_request(date.today(), needed_time=time(0, 0, 1))
        self._approve(req)
        req.refresh_from_db()
        self.assertIsNotNone(req.zone_sync_applied_at)
        self.machine.refresh_from_db()
        self.assertEqual(self.machine.current_working_zone_id, self.to_zone.id)


class ZoneSyncSchedulingTests(TestCase):
    """AssetPlanningRequest._sync_zone_sync_schedule(): the one-off task this
    replaces the every-5-minutes poll with — see apply_scheduled_zone_change."""

    @classmethod
    def setUpTestData(cls):
        cls.company = make_company()
        cls.from_zone = make_zone(cls.company, name="From Zone")
        cls.to_zone = make_zone(cls.company, name="To Zone")
        cls.laborer = make_employee(cls.company, make_user(username="scheduling_laborer"))
        cls.superuser = User.objects.create_superuser(
            username="scheduling_admin", password="testpass123", email="sched@a.com",
        )
        make_employee(cls.company, cls.superuser)

    def _make_request(self, needed_date, needed_time=None):
        req = AssetPlanningRequest(
            company=self.company,
            service_type=SERVICE_TYPE_PEONES,
            to_zone=self.to_zone,
            from_zone=self.from_zone,
            needed_date=needed_date,
            needed_time=needed_time,
        )
        req.full_clean(exclude=["planning_type"])
        req.save()
        req.moved_employees.set([self.laborer])
        return req

    def _approve(self, req):
        request = RequestFactory().post("/")
        SessionMiddleware(lambda r: None).process_request(request)
        request.session.save()
        MessageMiddleware(lambda r: None).process_request(request)
        request.user = self.superuser
        AssetPlanningRequestAdmin(AssetPlanningRequest, admin_site=None).approve_submit_action(request, req)

    def test_approving_future_request_schedules_one_off_task(self):
        req = self._make_request(date.today() + timedelta(days=3), needed_time=time(9, 0))
        self._approve(req)
        req.refresh_from_db()

        task = PeriodicTask.objects.get(name=req.zone_sync_task_name)
        self.assertTrue(task.one_off)
        self.assertTrue(task.enabled)
        self.assertEqual(task.task, AssetPlanningRequest.ZONE_SYNC_TASK_PATH)
        self.assertEqual(json.loads(task.args), [req.pk])
        expected_run_at = timezone.make_aware(datetime.combine(req.needed_date, req.needed_time))
        self.assertEqual(task.clocked.clocked_time, expected_run_at)

    def test_approving_due_request_applies_immediately_without_scheduling(self):
        req = self._make_request(date.today())
        self._approve(req)
        req.refresh_from_db()
        self.assertIsNotNone(req.zone_sync_applied_at)
        self.assertFalse(PeriodicTask.objects.filter(name=req.zone_sync_task_name).exists())

    def test_editing_after_approval_cancels_the_scheduled_task(self):
        req = self._make_request(date.today() + timedelta(days=3))
        self._approve(req)
        self.assertTrue(PeriodicTask.objects.filter(name=req.zone_sync_task_name).exists())

        # A review-tracked field edit auto-reverts status to pending (see
        # AssetPlanningRequest.save()) — the now-stale schedule must go too.
        req.needed_date = date.today() + timedelta(days=5)
        req.save()
        req.refresh_from_db()
        self.assertEqual(req.status, AssetPlanningRequestStatus.PENDING_REVIEW)
        self.assertFalse(PeriodicTask.objects.filter(name=req.zone_sync_task_name).exists())

    def test_scheduled_task_applies_zone_change_once_due(self):
        req = self._make_request(date.today() + timedelta(days=3))
        self._approve(req)
        # Simulate the scheduled moment arriving.
        AssetPlanningRequest.objects.filter(pk=req.pk).update(needed_date=date.today() - timedelta(days=1))

        result = apply_scheduled_zone_change(req.pk)
        self.assertEqual(result["status"], "ok")
        req.refresh_from_db()
        self.assertIsNotNone(req.zone_sync_applied_at)
        self.laborer.refresh_from_db()
        self.assertEqual(self.laborer.current_working_zone_id, self.to_zone.id)

    def test_scheduled_task_skips_if_no_longer_approved(self):
        """Defensive re-check: save() cancels the schedule when a decision is
        reverted, but a task already in flight to the broker could still fire."""
        req = self._make_request(date.today() + timedelta(days=3))
        self._approve(req)
        AssetPlanningRequest.objects.filter(pk=req.pk).update(status=AssetPlanningRequestStatus.PENDING_REVIEW)

        result = apply_scheduled_zone_change(req.pk)
        self.assertEqual(result["status"], "skipped")
        self.laborer.refresh_from_db()
        self.assertIsNone(self.laborer.current_working_zone_id)

    def test_apply_zone_change_does_not_reapply(self):
        req = self._make_request(date.today())
        self._approve(req)
        other_zone = make_zone(self.company, name="Another Zone")
        AssetPlanningRequest.objects.filter(pk=req.pk).update(to_zone=other_zone)
        req.refresh_from_db()

        self.assertFalse(req.apply_zone_change())
        self.laborer.refresh_from_db()
        self.assertEqual(self.laborer.current_working_zone_id, self.to_zone.id)
