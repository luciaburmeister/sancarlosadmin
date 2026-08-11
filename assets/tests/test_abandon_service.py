"""
assets/tests/test_abandon_service.py

Covers the "No puedo hacer más" bulk-abandon flow: deriving where a cuba/porte
job was left (AssetPlanning.compute_abandoned_stage) and bulk-flipping a
driver's remaining today's plannings to not_completed (PlanningAbandoner).
"""
from datetime import date, timedelta

from django.test import TestCase
from django.utils import timezone

from api.fleet.tests.helpers import make_asset, make_asset_type, make_company, make_employee, make_user, make_zone
from assets.models import (
    AbandonedStageChoices,
    AssetPlanning,
    AssetTransaction,
    PlanningStatus,
    PlanningType,
    TransactionTypeChoices,
)
from assets.services.abandon import PlanningAbandoner
from company.models import WorkingZone


class ComputeAbandonedStageCubaTests(TestCase):
    def setUp(self):
        self.company = make_company()
        self.user = make_user()
        self.employee = make_employee(self.company, self.user)
        self.asset_type = make_asset_type(self.company)
        self.obra = make_zone(self.company, name="Obra")
        self.obra.zone_type = WorkingZone.ZoneType.OBRA
        self.obra.save()
        self.asset = make_asset(self.company, self.asset_type, employee=self.employee, zone=self.obra)
        self.planning = AssetPlanning.objects.create(
            company=self.company, asset=self.asset, assigned_to=self.employee,
            scheduled_date=date.today(), planning_type=PlanningType.CUBA, status=PlanningStatus.PENDING,
        )

    def _make_tx(self, transaction_type, from_zone=None, data=None, minutes_ago=0):
        return AssetTransaction.objects.create(
            company=self.company, asset=self.asset, from_zone=from_zone,
            transaction_type=transaction_type,
            date=timezone.now() - timedelta(minutes=minutes_ago),
            data=data or {},
        )

    def test_no_transactions_means_not_started(self):
        self.assertEqual(self.planning.compute_abandoned_stage(), AbandonedStageChoices.NOT_STARTED)

    def test_picked_up_with_vertedero_pending_pour(self):
        self._make_tx(TransactionTypeChoices.MOVEMENT, from_zone=self.obra, data={"vertedero": "Vertedero X"})
        self.assertEqual(self.planning.compute_abandoned_stage(), AbandonedStageChoices.PICKED_UP_PENDING_POUR)

    def test_picked_up_verteo_done_pending_delivery(self):
        self._make_tx(TransactionTypeChoices.MOVEMENT, from_zone=self.obra, data={"vertedero": "Vertedero X"}, minutes_ago=30)
        self._make_tx(TransactionTypeChoices.RECEIPT, minutes_ago=10)
        self.assertEqual(self.planning.compute_abandoned_stage(), AbandonedStageChoices.PICKED_UP_PENDING_DELIVERY)

    def test_picked_up_no_vertedero_required_pending_delivery(self):
        self._make_tx(TransactionTypeChoices.MOVEMENT, from_zone=self.obra, data={"vertedero": ""})
        self.assertEqual(self.planning.compute_abandoned_stage(), AbandonedStageChoices.PICKED_UP_PENDING_DELIVERY)

    def test_delivered_pending_confirmation(self):
        self._make_tx(TransactionTypeChoices.MOVEMENT, from_zone=self.obra, data={"vertedero": ""}, minutes_ago=30)
        self._make_tx(TransactionTypeChoices.DELIVERY, minutes_ago=10)
        self.assertEqual(self.planning.compute_abandoned_stage(), AbandonedStageChoices.DELIVERED_PENDING_CONFIRMATION)

    def test_prefers_fk_linked_transaction_over_bare_asset_correlation(self):
        # An older, unlinked transaction for the same asset (e.g. a previous day's
        # job) must not shadow this planning's own linked transaction.
        AssetTransaction.objects.create(
            company=self.company, asset=self.asset, transaction_type=TransactionTypeChoices.DELIVERY,
            date=timezone.now() - timedelta(days=1), data={},
        )
        AssetTransaction.objects.create(
            company=self.company, asset=self.asset, from_zone=self.obra,
            transaction_type=TransactionTypeChoices.MOVEMENT, date=timezone.now(),
            data={"vertedero": "Vertedero X"}, planning=self.planning,
        )
        self.assertEqual(self.planning.compute_abandoned_stage(), AbandonedStageChoices.PICKED_UP_PENDING_POUR)


class ComputeAbandonedStagePorteTests(TestCase):
    def setUp(self):
        self.company = make_company()
        self.user = make_user()
        self.employee = make_employee(self.company, self.user)

    def _make_planning(self, status):
        return AssetPlanning.objects.create(
            company=self.company, assigned_to=self.employee,
            scheduled_date=date.today(), planning_type=PlanningType.PORTE, status=status,
        )

    def test_pending_porte_is_not_started(self):
        planning = self._make_planning(PlanningStatus.PENDING)
        self.assertEqual(planning.compute_abandoned_stage(), AbandonedStageChoices.NOT_STARTED)

    def test_in_transit_porte_is_pending_delivery(self):
        planning = self._make_planning(PlanningStatus.IN_TRANSIT)
        self.assertEqual(planning.compute_abandoned_stage(), AbandonedStageChoices.PICKED_UP_PENDING_DELIVERY)


class AbandonRemainingPlanningsTests(TestCase):
    def setUp(self):
        self.company = make_company()
        self.user = make_user()
        self.employee = make_employee(self.company, self.user)
        self.other_user = make_user(username="other")
        self.other_employee = make_employee(self.company, self.other_user)

    def _make_planning(self, employee=None, planning_type=PlanningType.PORTE, status=PlanningStatus.PENDING, scheduled_date=None):
        return AssetPlanning.objects.create(
            company=self.company, assigned_to=employee or self.employee,
            scheduled_date=scheduled_date or date.today(),
            planning_type=planning_type, status=status,
        )

    def test_abandons_only_my_open_cuba_and_porte_plannings_today(self):
        mine_porte = self._make_planning(planning_type=PlanningType.PORTE, status=PlanningStatus.PENDING)
        mine_cuba = self._make_planning(planning_type=PlanningType.CUBA, status=PlanningStatus.IN_TRANSIT)
        others = self._make_planning(employee=self.other_employee, planning_type=PlanningType.PORTE, status=PlanningStatus.PENDING)
        yesterday = self._make_planning(planning_type=PlanningType.PORTE, status=PlanningStatus.PENDING, scheduled_date=date.today() - timedelta(days=1))
        machine_hours = self._make_planning(planning_type=PlanningType.MACHINE_HOURS, status=PlanningStatus.PENDING)
        already_delivered = self._make_planning(planning_type=PlanningType.PORTE, status=PlanningStatus.DELIVERED)

        abandoned = PlanningAbandoner().abandon_remaining(self.employee)

        abandoned_ids = {p.id for p in abandoned}
        self.assertEqual(abandoned_ids, {mine_porte.id, mine_cuba.id})
        for p in (mine_porte, mine_cuba):
            p.refresh_from_db()
            self.assertEqual(p.status, PlanningStatus.NOT_COMPLETED)
            self.assertIsNotNone(p.abandoned_at)
            self.assertTrue(p.abandoned_stage)
        for p in (others, yesterday, machine_hours, already_delivered):
            p.refresh_from_db()
            self.assertNotEqual(p.status, PlanningStatus.NOT_COMPLETED)

    def test_idempotent_second_call_finds_nothing(self):
        self._make_planning(planning_type=PlanningType.PORTE, status=PlanningStatus.PENDING)
        first = PlanningAbandoner().abandon_remaining(self.employee)
        second = PlanningAbandoner().abandon_remaining(self.employee)
        self.assertEqual(len(first), 1)
        self.assertEqual(len(second), 0)
