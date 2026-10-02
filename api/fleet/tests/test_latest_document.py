"""
api/fleet/tests/test_latest_document.py

Tests for LatestTransactionDocumentView — GET /en/api/transactions/latest-document/
This is what the Android "Abrir Documento" button calls to pull the document+QR
for the most recent cuba pickup/verteo/porte pickup the logged-in driver did.
"""
import json

from django.utils import timezone
from datetime import timedelta
from rest_framework.test import APIClient, APITestCase

from assets.models import AssetTransaction, TransactionTypeChoices
from api.fleet.tests.helpers import (
    make_company,
    make_user,
    make_employee,
    make_asset_type,
    make_asset,
    make_zone,
)


class LatestTransactionDocumentViewTests(APITestCase):
    URL = "/en/api/transactions/latest-document/"

    def setUp(self):
        self.company = make_company()
        self.user = make_user()
        self.employee = make_employee(self.company, self.user)
        self.asset_type = make_asset_type(self.company)
        self.zone = make_zone(self.company)
        self.asset = make_asset(self.company, self.asset_type, employee=self.employee, zone=self.zone)
        self.client = APIClient()

    def _make_tx(self, transaction_type, minutes_ago=0, **overrides):
        return AssetTransaction.objects.create(
            asset=self.asset,
            company=self.company,
            performed_by=self.employee,
            transaction_type=transaction_type,
            date=timezone.now() - timedelta(minutes=minutes_ago),
            **overrides,
        )

    def test_requires_authentication(self):
        """No auth -> 401/403, not a 200 or a server error."""
        response = self.client.get(self.URL)
        self.assertIn(response.status_code, (401, 403))

    def test_no_documents_yet_returns_404(self):
        self.client.force_authenticate(user=self.user)
        response = self.client.get(self.URL)
        self.assertEqual(response.status_code, 404)
        self.assertIn("error", json.loads(response.content))

    def test_returns_the_single_eligible_transaction(self):
        tx = self._make_tx(TransactionTypeChoices.MOVEMENT)
        self.client.force_authenticate(user=self.user)
        response = self.client.get(self.URL)
        self.assertEqual(response.status_code, 200)
        body = json.loads(response.content)
        self.assertEqual(body["transaction_id"], tx.id)
        self.assertEqual(body["transaction_type"], TransactionTypeChoices.MOVEMENT)
        self.assertTrue(body["document_url"])
        self.assertIn(tx.public_token, body["document_url"])

    def test_returns_most_recent_of_the_three_document_types(self):
        """A verteo (RECEIPT) that closes out a load must win over an older
        cuba pickup (MOVEMENT) -- it's proof of what was just dumped."""
        self._make_tx(TransactionTypeChoices.MOVEMENT, minutes_ago=60)
        newest = self._make_tx(TransactionTypeChoices.RECEIPT, minutes_ago=5)
        self.client.force_authenticate(user=self.user)
        response = self.client.get(self.URL)
        body = json.loads(response.content)
        self.assertEqual(body["transaction_id"], newest.id)
        self.assertEqual(body["transaction_type"], TransactionTypeChoices.RECEIPT)

    def test_porte_movement_is_eligible(self):
        tx = self._make_tx(TransactionTypeChoices.PORTE_MOVEMENT)
        self.client.force_authenticate(user=self.user)
        response = self.client.get(self.URL)
        body = json.loads(response.content)
        self.assertEqual(body["transaction_id"], tx.id)

    def test_ignores_transaction_types_outside_the_document_set(self):
        """DELIVERY, MACHINE_HOURS etc. are never a document to show, even
        when they're the most recent thing the driver did."""
        older = self._make_tx(TransactionTypeChoices.MOVEMENT, minutes_ago=60)
        self._make_tx(TransactionTypeChoices.DELIVERY, minutes_ago=1)
        self._make_tx(TransactionTypeChoices.MACHINE_HOURS, minutes_ago=1)
        self.client.force_authenticate(user=self.user)
        response = self.client.get(self.URL)
        body = json.loads(response.content)
        self.assertEqual(body["transaction_id"], older.id)

    def test_only_returns_the_authenticated_employees_own_transactions(self):
        other_user = make_user(username="otherdriver")
        other_employee = make_employee(self.company, other_user)
        self._make_tx(TransactionTypeChoices.MOVEMENT, minutes_ago=1)  # belongs to self.employee
        AssetTransaction.objects.create(
            asset=self.asset,
            company=self.company,
            performed_by=other_employee,
            transaction_type=TransactionTypeChoices.RECEIPT,
            date=timezone.now(),
        )
        self.client.force_authenticate(user=other_user)
        response = self.client.get(self.URL)
        body = json.loads(response.content)
        self.assertEqual(json.loads(response.content)["transaction_id"], AssetTransaction.objects.get(performed_by=other_employee).id)
        self.assertNotEqual(body["transaction_id"], AssetTransaction.objects.get(performed_by=self.employee).id)
