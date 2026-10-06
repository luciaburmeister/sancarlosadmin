"""
api/fleet/tests/test_latest_document.py

Tests for LatestTransactionDocumentView — GET /en/api/transactions/latest-document/
This is what the Android "Abrir Documento" button calls to pull the document+QR
for the most recent cuba pickup/verteo/porte load the logged-in driver did.
"""
import json

from django.utils import timezone
from datetime import timedelta
from rest_framework.test import APIClient, APITestCase

from assets.models import AssetTransaction, AssetTransactionReceipt, TransactionTypeChoices
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

    def test_porte_load_is_eligible(self):
        tx = self._make_tx(TransactionTypeChoices.PORTE_LOAD)
        self.client.force_authenticate(user=self.user)
        response = self.client.get(self.URL)
        body = json.loads(response.content)
        self.assertEqual(body["transaction_id"], tx.id)
        self.assertEqual(body["transaction_type"], TransactionTypeChoices.PORTE_LOAD)

    def test_porte_load_wins_over_its_earlier_porte_pickup(self):
        """The porte document is the load (carga) -- it carries the goods data.
        The recogida before it is never the document, even if it's newer than
        everything else."""
        self._make_tx(TransactionTypeChoices.PORTE_MOVEMENT, minutes_ago=30)
        load = self._make_tx(TransactionTypeChoices.PORTE_LOAD, minutes_ago=10)
        self.client.force_authenticate(user=self.user)
        response = self.client.get(self.URL)
        body = json.loads(response.content)
        self.assertEqual(body["transaction_id"], load.id)

    def test_porte_movement_is_not_a_document(self):
        older = self._make_tx(TransactionTypeChoices.MOVEMENT, minutes_ago=60)
        self._make_tx(TransactionTypeChoices.PORTE_MOVEMENT, minutes_ago=1)
        self.client.force_authenticate(user=self.user)
        response = self.client.get(self.URL)
        body = json.loads(response.content)
        self.assertEqual(body["transaction_id"], older.id)

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

    def test_receipt_includes_the_related_movement_it_discharges(self):
        """A verteo must also surface the cuba pickup it discharges -- legally
        the inspector needs to see both documents."""
        movement = self._make_tx(TransactionTypeChoices.MOVEMENT, minutes_ago=60)
        receipt_tx = self._make_tx(TransactionTypeChoices.RECEIPT, minutes_ago=5)
        AssetTransactionReceipt.objects.create(
            transaction=receipt_tx, nro_albaran="ALB-1", movement_transaction=movement,
        )
        self.client.force_authenticate(user=self.user)
        response = self.client.get(self.URL)
        body = json.loads(response.content)
        self.assertEqual(body["related_transaction_id"], movement.id)
        self.assertEqual(body["related_transaction_type"], TransactionTypeChoices.MOVEMENT)
        self.assertIn(movement.public_token, body["related_document_url"])

    def test_receipt_without_a_linked_movement_omits_related_keys(self):
        receipt_tx = self._make_tx(TransactionTypeChoices.RECEIPT)
        AssetTransactionReceipt.objects.create(
            transaction=receipt_tx, nro_albaran="ALB-1", movement_transaction=None,
        )
        self.client.force_authenticate(user=self.user)
        response = self.client.get(self.URL)
        body = json.loads(response.content)
        self.assertNotIn("related_transaction_id", body)
        self.assertNotIn("related_transaction_type", body)
        self.assertNotIn("related_document_url", body)

    def test_movement_type_never_includes_related_keys(self):
        """Only RECEIPT ever gets the related-movement fields -- MOVEMENT and
        PORTE_LOAD must never send them, not even as null."""
        self._make_tx(TransactionTypeChoices.MOVEMENT)
        self.client.force_authenticate(user=self.user)
        response = self.client.get(self.URL)
        body = json.loads(response.content)
        self.assertNotIn("related_transaction_id", body)
        self.assertNotIn("related_document_url", body)

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
