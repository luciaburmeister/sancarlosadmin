"""
api/fleet/tests/test_assets_n_plus_one.py

Regression coverage for the Sentry N+1 Query alerts on GET /es/api/assets/
(AssetsListView issuing one AssetTransaction query per asset).

Two things are guarded here:
1. The list endpoint's query count stays flat as the number of assets grows
   (the actual fix: AssetsListView now prefetches `transactions`).
2. The *single*-asset lookups that San Carlos's write flows use (claiming a
   pending verteo, delivery checks, etc. — AssetAssignView, DeliveryCheckView)
   never prefetch `transactions`, so they must keep costing the same small,
   constant number of queries as before this fix — not grow with the asset's
   full transaction history. This is what makes the fix safe for San Carlos.
"""

from datetime import timedelta

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from assets.models import Asset, AssetTransaction, CubaFillState, CubaTransactionKey, TransactionTypeChoices
from api.fleet.tests.helpers import make_company, make_user, make_employee, make_asset_type, make_asset


class AssetsListNPlusOneTests(TestCase):
    """GET /es/api/assets/ must not issue one AssetTransaction query per asset."""

    @classmethod
    def setUpTestData(cls):
        cls.company = make_company()
        cls.user = make_user()
        cls.employee = make_employee(cls.company, cls.user)
        cls.asset_type = make_asset_type(cls.company)

    def _add_assets_with_transactions(self, count, name_offset):
        for i in range(count):
            asset = make_asset(self.company, self.asset_type, employee=self.employee, name=f"CUBA-{name_offset + i:03d}")
            AssetTransaction.objects.create(
                company=self.company, asset=asset,
                transaction_type=TransactionTypeChoices.MOVEMENT,
                performed_by=self.employee,
            )
            AssetTransaction.objects.create(
                company=self.company, asset=asset,
                transaction_type=TransactionTypeChoices.RECEIPT,
                performed_by=self.employee,
            )

    def test_query_count_does_not_scale_with_asset_count(self):
        client = APIClient()
        client.force_authenticate(user=self.user)
        url = reverse("assets")

        self._add_assets_with_transactions(2, name_offset=0)
        with self.assertNumQueries(3):
            response = client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()["assets"]), 2)

        self._add_assets_with_transactions(8, name_offset=2)
        with self.assertNumQueries(3):
            response = client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()["assets"]), 10)


class AssetSingleLookupQueryCountTests(TestCase):
    """San Carlos fetches one Asset at a time (never prefetching `transactions`)
    for AssetAssignView, DeliveryCheckView, etc. These lookups must keep
    costing the same handful of queries regardless of how much transaction
    history the asset has accumulated — not materialize the whole history."""

    @classmethod
    def setUpTestData(cls):
        cls.company = make_company()
        cls.user = make_user()
        cls.employee = make_employee(cls.company, cls.user)
        cls.asset_type = make_asset_type(cls.company)

    def _fresh_asset_with_history(self, history_size):
        asset = make_asset(self.company, self.asset_type, employee=self.employee)
        base = timezone.now() - timedelta(days=1)
        for i in range(history_size):
            AssetTransaction.objects.create(
                company=self.company, asset=asset,
                transaction_type=TransactionTypeChoices.MOVEMENT if i % 2 == 0 else TransactionTypeChoices.RECEIPT,
                performed_by=self.employee,
                date=base + timedelta(minutes=i),  # strictly increasing, deterministic ordering
            )
        # Reload so the instance has no `transactions` prefetch cache,
        # matching how AssetAssignView/DeliveryCheckView fetch a single Asset.
        return Asset.objects.get(pk=asset.pk)

    def test_has_pending_receipt_query_count_independent_of_history_size(self):
        asset = self._fresh_asset_with_history(50)
        with self.assertNumQueries(2):
            asset.has_pending_receipt

    def test_last_movement_transaction_query_count_independent_of_history_size(self):
        asset = self._fresh_asset_with_history(50)
        with self.assertNumQueries(1):
            asset.last_movement_transaction

    def test_current_transaction_status_query_count_independent_of_history_size(self):
        asset = self._fresh_asset_with_history(50)
        # Most recent transaction (i=49) is RECEIPT -> one recursion step to
        # find the preceding non-receipt transaction: 2 queries, not 50.
        with self.assertNumQueries(2):
            asset.current_transaction_status()

    def test_verteo_pending_transaction_query_count_independent_of_history_size(self):
        asset = self._fresh_asset_with_history(0)
        AssetTransaction.objects.create(
            company=self.company, asset=asset,
            transaction_type=TransactionTypeChoices.MOVEMENT,
            performed_by=self.employee,
            data={CubaTransactionKey.ESTADO: CubaFillState.FULL},
        )
        asset = Asset.objects.get(pk=asset.pk)
        with self.assertNumQueries(2):
            asset.verteo_pending_transaction
