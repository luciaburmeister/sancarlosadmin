"""
api/fleet/tests/test_endpoints.py

Tests 26-34: POST /en/api/assets/<id>/transaction/

Run just this file:
    python manage.py test api.fleet.tests.test_endpoints
"""

import json
from unittest.mock import patch

from django.test import TestCase
from rest_framework.test import APIClient

from assets.models import AssetTransaction, TransactionTypeChoices
from api.fleet.tests.helpers import (
    make_company,
    make_user,
    make_employee,
    make_asset_type,
    make_asset,
    make_zone,
    make_planning,
)


class AssetTransactionEndpointTests(TestCase):
    """
    Tests for POST /en/api/assets/<id>/transaction/
    These use APIClient to simulate real HTTP requests from the Android app.
    """

    def setUp(self):
        self.client = APIClient()
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
        self.planning = make_planning(self.company, asset=self.asset, assigned_to=self.employee)
        self.client.force_authenticate(user=self.user)

    def _movement_payload(self, **overrides):
        payload = {
            "transaction_type": "movement",
            "latitude": 40.416775,
            "longitude": -3.703790,
            "matricula": "1234ABC",
            "tipo_camion": "Volquete",
            "implemento": "Gancho",
            "cliente": "Cliente Test",
            "obra": "Obra Test",
            "trabajo": "Recogida",
            "tipo_cuba": "6m3",
            "m3": "6",
            "vertedero": "",
            "zone_id": self.zone.id,
            "planning_id": self.planning.id,
        }
        payload.update(overrides)
        return payload

    def _url(self, asset_id=None):
        pk = asset_id if asset_id is not None else self.asset.id
        return f"/en/api/assets/{pk}/transaction/"

    # Test 26
    def test_unauthenticated_request_is_rejected(self):
        """
        A request with no JWT token must get a 401 Unauthorized response.
        This confirms the endpoint is not accidentally public.
        """
        self.client.force_authenticate(user=None)
        response = self.client.post(
            self._url(),
            data=json.dumps(self._movement_payload()),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 401)

    # Test 27
    def test_movement_endpoint_returns_200(self):
        """A valid movement payload from an authenticated user must return 200."""
        with patch("api.fleet.views.assets.send_asset_transaction_email_task.delay"):
            response = self.client.post(
                self._url(),
                data=json.dumps(self._movement_payload()),
                content_type="application/json",
            )
        self.assertEqual(response.status_code, 200)

    # Test 28
    def test_delivery_endpoint_returns_200(self):
        """A valid delivery payload must return 200."""
        with patch("api.fleet.views.assets.send_asset_transaction_email_task.delay"):
            response = self.client.post(
                self._url(),
                data=json.dumps(self._movement_payload(transaction_type="delivery")),
                content_type="application/json",
            )
        self.assertEqual(response.status_code, 200)

    # Test 29
    def test_receipt_endpoint_returns_200(self):
        """A valid receipt/verteo payload must return 200."""
        AssetTransaction.objects.create(
            asset=self.asset,
            company=self.company,
            transaction_type=TransactionTypeChoices.MOVEMENT,
            performed_by=self.employee,
        )
        response = self.client.post(
            self._url(),
            data=json.dumps({
                "transaction_type": "receipt",
                "nro_albaran": "ALB-001",
                "peso_neto": "500",
                "material_vertido": "RCD Limpio",
                "latitude": 40.416775,
                "longitude": -3.703790,
                "zone_id": self.zone.id,
                "planning_id": self.planning.id,
            }),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)

    # Test 30
    def test_wrong_asset_id_returns_404(self):
        """Calling the endpoint with a non-existent asset ID must return 404."""
        response = self.client.post(
            self._url(asset_id=99999),
            data=json.dumps(self._movement_payload()),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 404)

    # Test 31
    def test_response_contains_success_true(self):
        """The response JSON must contain 'success': true."""
        with patch("api.fleet.views.assets.send_asset_transaction_email_task.delay"):
            response = self.client.post(
                self._url(),
                data=json.dumps(self._movement_payload()),
                content_type="application/json",
            )
        self.assertTrue(response.json().get("success"))

    # Test 32
    def test_response_contains_transaction_id(self):
        """The response JSON must contain a transaction_id — the app uses this."""
        with patch("api.fleet.views.assets.send_asset_transaction_email_task.delay"):
            response = self.client.post(
                self._url(),
                data=json.dumps(self._movement_payload()),
                content_type="application/json",
            )
        self.assertIn("transaction_id", response.json())

    # Test 33
    def test_movement_endpoint_creates_exactly_one_transaction(self):
        """Calling the endpoint once must create exactly one transaction row."""
        with patch("api.fleet.views.assets.send_asset_transaction_email_task.delay"):
            self.client.post(
                self._url(),
                data=json.dumps(self._movement_payload()),
                content_type="application/json",
            )
        self.assertEqual(AssetTransaction.objects.count(), 1)

    # Test 34
    def test_invalid_transaction_type_returns_400(self):
        """Sending an unknown transaction_type must return 400, not 500."""
        response = self.client.post(
            self._url(),
            data=json.dumps(self._movement_payload(transaction_type="BLABLA")),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)

    def test_movement_without_planning_id_returns_400(self):
        """Cuba can't operate without a plan — strict mode."""
        payload = self._movement_payload()
        del payload["planning_id"]
        response = self.client.post(
            self._url(),
            data=json.dumps(payload),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("error", response.json())
