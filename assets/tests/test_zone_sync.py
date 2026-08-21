"""
assets/tests/test_zone_sync.py

Tests for automatic current_working_zone sync (Asset + Employee) driven by:
  - AssetTransaction._sync_zones_on_porte_delivery(): fires on creation of a
    PORTE_DELIVERY transaction linked to an Own Machine Transport planning.
  - AssetPlanningRequest.apply_machinist_zone_change() / the Machinist
    Movement service_type approve flow (no AssetPlanning involved).
  - assets.tasks.apply_due_machinist_movements: daily Celery beat pickup for
    future-dated approved Machinist Movement requests.

See assets/models.py, assets/admin/asset_planning_request.py, assets/tasks.py.

Run just this file:
    python manage.py test assets.tests.test_zone_sync
"""
from datetime import date, timedelta

from django.contrib.auth import get_user_model
from django.contrib.messages.middleware import MessageMiddleware
from django.contrib.sessions.middleware import SessionMiddleware
from django.test import RequestFactory, TestCase

from assets.admin.asset_planning_request import AssetPlanningRequestAdmin
from assets.constants import SERVICE_TYPE_MAQUINA_PROPIA, SERVICE_TYPE_MAQUINISTAS, SERVICE_TYPE_MATERIAL_EXTERNO
from assets.models import AssetPlanningRequest, AssetPlanningRequestStatus, AssetTransaction, TransactionTypeChoices
from assets.tasks import apply_due_machinist_movements
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
        self.assertEqual(req.planning_type, "none")

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


class ApplyDueMachinistMovementsTaskTests(TestCase):
    def setUp(self):
        self.company = make_company()
        self.from_zone = make_zone(self.company, name="From Zone")
        self.to_zone = make_zone(self.company, name="To Zone")
        client = Client.objects.create(name="Client 1", company=self.company)
        self.project_file = ProjectFile.objects.create(name="Project 1", client=client, company=self.company)
        self.machinist = make_employee(self.company, make_user(username="machinist2"))

    def _make_approved_request(self, needed_date):
        req = AssetPlanningRequest.objects.create(
            company=self.company,
            service_type=SERVICE_TYPE_MAQUINISTAS,
            to_zone=self.to_zone,
            from_zone=self.from_zone,
            needed_date=needed_date,
            project_file=self.project_file,
            status=AssetPlanningRequestStatus.APPROVED,
        )
        req.moved_employees.set([self.machinist])
        return req

    def test_task_applies_due_request(self):
        req = self._make_approved_request(date.today() - timedelta(days=1))
        apply_due_machinist_movements()
        req.refresh_from_db()
        self.assertIsNotNone(req.zone_sync_applied_at)
        self.machinist.refresh_from_db()
        self.assertEqual(self.machinist.current_working_zone_id, self.to_zone.id)

    def test_task_ignores_not_yet_due_request(self):
        req = self._make_approved_request(date.today() + timedelta(days=1))
        apply_due_machinist_movements()
        req.refresh_from_db()
        self.assertIsNone(req.zone_sync_applied_at)
        self.machinist.refresh_from_db()
        self.assertIsNone(self.machinist.current_working_zone_id)

    def test_task_does_not_reapply_already_applied_request(self):
        req = self._make_approved_request(date.today())
        req.apply_machinist_zone_change()
        other_zone = make_zone(self.company, name="Another Zone")
        req.to_zone = other_zone
        req.save(update_fields=["to_zone"])
        apply_due_machinist_movements()
        self.machinist.refresh_from_db()
        self.assertEqual(self.machinist.current_working_zone_id, self.to_zone.id)
