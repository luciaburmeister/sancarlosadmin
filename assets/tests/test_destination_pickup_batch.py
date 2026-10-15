from datetime import timedelta

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from api.fleet.tests.helpers import make_asset, make_asset_type, make_company
from assets.models import (
    AssetPlanning,
    AssetTransaction,
    AssetTransactionReceipt,
    ChecklistStepKey,
    ChecklistStepStatus,
    CubaFillState,
    CubaOperationType,
    CubaTransactionKey,
    PlanningStatus,
    PlanningType,
    TransactionTypeChoices,
)
from company.models import WorkingZone


class DestinationPickupBatchTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = make_company()
        cls.asset = make_asset(cls.company, make_asset_type(cls.company))
        cls.obra = WorkingZone.objects.create(
            company=cls.company, name="Obra", zone_type=WorkingZone.ZoneType.OBRA,
        )
        cls.almacen = WorkingZone.objects.create(
            company=cls.company, name="Almacen", zone_type=WorkingZone.ZoneType.ALMACEN,
        )
        cls.now = timezone.now()

    def _planning(self, operation):
        return AssetPlanning.objects.create(
            company=self.company,
            asset=self.asset,
            destination=self.obra,
            scheduled_date=self.now.date(),
            scheduled_time=self.now.time(),
            status=PlanningStatus.IN_TRANSIT,
            planning_type=PlanningType.CUBA,
            cuba_operation_type=operation,
        )

    def _movement(self, planning, zone, *, hours_ago, full):
        return AssetTransaction.objects.create(
            company=self.company,
            asset=self.asset,
            planning=planning,
            from_zone=zone,
            transaction_type=TransactionTypeChoices.MOVEMENT,
            date=self.now - timedelta(hours=hours_ago),
            data={CubaTransactionKey.ESTADO: CubaFillState.FULL} if full else {},
        )

    def test_batch_matches_the_per_planning_pickup_and_verteo_flag(self):
        newest_wins = self._planning(CubaOperationType.RETIRADA)
        self._movement(newest_wins, self.obra, hours_ago=3, full=True)
        latest = self._movement(newest_wins, self.obra, hours_ago=1, full=False)

        already_verted = self._planning(CubaOperationType.RETIRADA)
        verted_pickup = self._movement(already_verted, self.obra, hours_ago=2, full=True)
        AssetTransactionReceipt.objects.create(
            transaction=verted_pickup,
            movement_transaction=verted_pickup,
            nro_albaran="0101",
        )

        cambio = self._planning(CubaOperationType.CAMBIO)
        self._movement(cambio, self.almacen, hours_ago=1, full=True)
        obra_pickup = self._movement(cambio, self.obra, hours_ago=4, full=True)

        plannings = list(AssetPlanning.objects.filter(
            pk__in=[newest_wins.pk, already_verted.pk, cambio.pk],
        ).select_related("destination"))
        with CaptureQueriesContext(connection) as loaded:
            AssetPlanning.attach_destination_pickups(plannings)
        with CaptureQueriesContext(connection) as reads:
            flags = {planning.pk: planning.has_pending_verteo() for planning in plannings}
            pickups = {
                planning.pk: planning._destination_pickup_transaction().pk
                for planning in plannings
            }
            checklists = {planning.pk: planning.cuba_checklist() for planning in plannings}

        self.assertEqual(len([
            query for query in loaded.captured_queries
            if '"assets_assettransaction"' in query["sql"]
        ]), 1)
        self.assertEqual(len([
            query for query in loaded.captured_queries
            if '"assets_assettransactionreceipt"' in query["sql"]
        ]), 1)
        self.assertEqual(reads.captured_queries, [])
        self.assertEqual(flags, {
            newest_wins.pk: False,
            already_verted.pk: False,
            cambio.pk: True,
        })
        self.assertEqual(pickups, {
            newest_wins.pk: latest.pk,
            already_verted.pk: verted_pickup.pk,
            cambio.pk: obra_pickup.pk,
        })
        self.assertEqual(
            [step["status"] for step in checklists[cambio.pk] if step["key"] == ChecklistStepKey.VERTER],
            [ChecklistStepStatus.PENDING],
        )

        for planning in plannings:
            fresh = AssetPlanning.objects.select_related("destination").get(pk=planning.pk)
            self.assertEqual(fresh.has_pending_verteo(), flags[planning.pk])
            self.assertEqual(fresh._destination_pickup_transaction().pk, pickups[planning.pk])
            self.assertEqual(fresh.cuba_checklist(), checklists[planning.pk])
