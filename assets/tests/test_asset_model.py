"""
api/fleet/tests/test_asset_model.py  (but lives in assets/tests/test_asset_model.py)

Tests 35-39: Asset.has_pending_receipt property
Tests 51-54: get_asset_status function

Note: These tests live in assets/tests/ not api/fleet/tests/
because they test models and functions in the assets app.

Run just this file:
    python manage.py test assets.tests.test_asset_model
"""

from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from assets.models import AssetTransaction, TransactionTypeChoices
from api.fleet.tests.helpers import (
    make_company,
    make_user,
    make_employee,
    make_asset_type,
    make_asset,
    make_zone,
)


class AssetModelTests(TestCase):
    """Tests for the has_pending_receipt property on the Asset model."""

    def setUp(self):
        self.company = make_company()
        self.user = make_user()
        self.employee = make_employee(self.company, self.user)
        self.asset_type = make_asset_type(self.company)
        self.zone = make_zone(self.company)
        self.asset = make_asset(
            self.company, self.asset_type,
            employee=self.employee,
            zone=self.zone,
        )

    def _make_transaction(self, tx_type, date_offset_seconds=0):
        """Helper to create a transaction with a specific type and time."""
        return AssetTransaction.objects.create(
            asset=self.asset,
            company=self.company,
            transaction_type=tx_type,
            performed_by=self.employee,
            date=timezone.now() + timedelta(seconds=date_offset_seconds),
        )

    # Test 35
    def test_has_pending_receipt_is_false_by_default(self):
        """
        A brand new asset with no transactions should have
        has_pending_receipt = False. The Verteo button should be available.
        """
        self.assertFalse(self.asset.has_pending_receipt)

    # Test 36
    def test_has_pending_receipt_is_true_after_receipt(self):
        """
        After a movement followed by a receipt (verteo),
        has_pending_receipt must return True — button greyed out.
        """
        self._make_transaction(TransactionTypeChoices.MOVEMENT, date_offset_seconds=0)
        self._make_transaction(TransactionTypeChoices.RECEIPT, date_offset_seconds=10)
        self.assertTrue(self.asset.has_pending_receipt)

    # Test 37
    def test_has_pending_receipt_is_false_after_movement_only(self):
        """
        After a movement with no receipt yet,
        has_pending_receipt must be False — verteo not done yet.
        """
        self._make_transaction(TransactionTypeChoices.MOVEMENT)
        self.assertFalse(self.asset.has_pending_receipt)

    # Test 38
    def test_has_pending_receipt_is_false_after_delivery(self):
        """A delivery transaction alone must not affect has_pending_receipt."""
        self._make_transaction(TransactionTypeChoices.DELIVERY)
        self.assertFalse(self.asset.has_pending_receipt)

    # Test 39
    def test_has_pending_receipt_with_movement_and_receipt(self):
        """Movement then receipt — property must be True."""
        self._make_transaction(TransactionTypeChoices.MOVEMENT, date_offset_seconds=0)
        self._make_transaction(TransactionTypeChoices.RECEIPT, date_offset_seconds=5)
        self.assertTrue(self.asset.has_pending_receipt)


class AssetStatusTests(TestCase):
    """
    Tests for get_asset_status — the function that determines
    whether a cuba is AVAILABLE, PICKED_UP, or DELIVERED
    based on its last transaction.
    """

    def setUp(self):
        self.company = make_company()
        self.user = make_user()
        self.employee = make_employee(self.company, self.user)
        self.asset_type = make_asset_type(self.company)
        self.zone = make_zone(self.company)
        self.asset = make_asset(self.company, self.asset_type, employee=self.employee)

    def _make_tx(self, tx_type, seconds_offset=0):
        return AssetTransaction.objects.create(
            asset=self.asset,
            company=self.company,
            transaction_type=tx_type,
            performed_by=self.employee,
            date=timezone.now() + timedelta(seconds=seconds_offset),
        )

    # Test 51
    def test_status_is_available_with_no_transactions(self):
        """A cuba with no transactions must show as AVAILABLE."""
        from api.fleet.views.assets import get_asset_status
        self.assertEqual(get_asset_status(None), "AVAILABLE")

    # Test 52
    def test_status_is_picked_up_after_movement(self):
        """After a recogida (MOVEMENT), status must be PICKED_UP."""
        from api.fleet.views.assets import get_asset_status
        tx = self._make_tx(TransactionTypeChoices.MOVEMENT)
        self.assertEqual(get_asset_status(tx), "PICKED_UP")

    # Test 53
    def test_status_is_delivered_after_delivery(self):
        """After a delivery (DELIVERY), status must be DELIVERED."""
        from api.fleet.views.assets import get_asset_status
        tx = self._make_tx(TransactionTypeChoices.DELIVERY)
        self.assertEqual(get_asset_status(tx), "DELIVERED")

    # Test 54
    def test_status_uses_latest_transaction_only(self):
        """
        When there are multiple transactions, only the most recent one
        determines the status.
        """
        from api.fleet.views.assets import get_asset_status
        self._make_tx(TransactionTypeChoices.MOVEMENT, seconds_offset=0)
        latest = self._make_tx(TransactionTypeChoices.DELIVERY, seconds_offset=10)
        self.assertEqual(get_asset_status(latest), "DELIVERED")
