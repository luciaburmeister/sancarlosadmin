"""
api/fleet/tests/test_historial.py

Tests 64-68: GET /en/api/movements/ transaction history endpoint

Run just this file:
    python manage.py test api.fleet.tests.test_historial
"""

from datetime import timedelta

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from assets.models import AssetTransaction, TransactionTypeChoices
from api.fleet.tests.helpers import (
    make_company,
    make_user,
    make_employee,
    make_asset_type,
    make_asset,
    make_zone,
)


class PorteHistoryTests(TestCase):
    """
    Tests for GET /en/api/movements/ — the transaction history
    the driver sees in the app.
    """

    def setUp(self):
        self.api_client = APIClient()
        self.company = make_company()
        self.user = make_user()
        self.employee = make_employee(self.company, self.user)
        self.asset_type = make_asset_type(self.company)
        self.zone = make_zone(self.company)
        self.asset = make_asset(self.company, self.asset_type, employee=self.employee)
        self.api_client.force_authenticate(user=self.user)

    def _make_tx(self, tx_type, seconds_offset=0):
        return AssetTransaction.objects.create(
            asset=self.asset,
            company=self.company,
            transaction_type=tx_type,
            performed_by=self.employee,
            date=timezone.now() + timedelta(seconds=seconds_offset),
            from_zone=self.zone,
        )

    # Test 64
    def test_movements_returns_all_transactions_for_employee(self):
        """
        Creating 3 transactions for this employee must result in
        3 records returned from the movements endpoint.
        """
        self._make_tx(TransactionTypeChoices.MOVEMENT, 0)
        self._make_tx(TransactionTypeChoices.DELIVERY, 10)
        self._make_tx(TransactionTypeChoices.RECEIPT, 20)

        response = self.api_client.get("/en/api/movements/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["count"], 3)

    # Test 65
    def test_movements_are_ordered_most_recent_first(self):
        """The most recent transaction must come first in the list."""
        self._make_tx(TransactionTypeChoices.MOVEMENT, seconds_offset=0)
        self._make_tx(TransactionTypeChoices.DELIVERY, seconds_offset=10)

        results = self.api_client.get("/en/api/movements/").json()["results"]
        self.assertEqual(results[0]["transaction_type"], TransactionTypeChoices.DELIVERY)

    # Test 66
    def test_movements_only_returns_own_employee_transactions(self):
        """
        Transactions performed by a different employee must not
        appear in this employee's history.
        """
        other_user = make_user(username="otheruser")
        other_employee = make_employee(self.company, other_user)
        AssetTransaction.objects.create(
            asset=self.asset,
            company=self.company,
            transaction_type=TransactionTypeChoices.MOVEMENT,
            performed_by=other_employee,
        )
        response = self.api_client.get("/en/api/movements/")
        self.assertEqual(response.json()["count"], 0)

    # Test 67
    def test_movements_returns_empty_when_no_transactions(self):
        """An employee with no transactions must get an empty list — not a crash."""
        response = self.api_client.get("/en/api/movements/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["count"], 0)

    # Test 68
    def test_unauthenticated_movements_request_is_rejected(self):
        """No token — movements endpoint must return 401."""
        self.api_client.force_authenticate(user=None)
        response = self.api_client.get("/en/api/movements/")
        self.assertEqual(response.status_code, 401)
