"""
api/fleet/tests/test_auth_required.py

Every fleet endpoint must reject an anonymous request with 401. The
per-file "no token" tests used to repeat this one check; this table is the
single place it lives now, and the route guard below makes sure a new
endpoint can't be added without being listed here (or explicitly exempted).

Run just this file:
    python manage.py test api.fleet.tests.test_auth_required
"""
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from api.fleet import urls as fleet_urls

GPS = {"latitude": 40.416775, "longitude": -3.703790}

# (method, url name, url args, payload). For GET the payload is sent as the
# query string, otherwise as a JSON body. Ids are placeholders: DRF checks
# permissions before the view body runs, so nothing is looked up.
ANONYMOUS_REQUESTS = [
    ("get", "assets", (), None),
    ("get", "asset_by_id", (1,), None),
    ("post", "asset_transaction", (1,), {"transaction_type": "movement", **GPS}),
    ("post", "assign", (1,), None),
    ("get", "cuba_suggestions", (1,), None),
    ("post", "reserve_cuba", (1,), {"asset_id": 1}),
    ("delete", "reserve_cuba", (1,), None),
    ("post", "asset_capacidad", (1,), {"capacidad": "6"}),
    ("post", "check_delivery_location", (1,), GPS),
    ("get", "machine_check", (1,), None),
    ("post", "clock_in", (), GPS),
    ("post", "clock_out", (), GPS),
    ("get", "attendance_status", (), None),
    ("post", "location-record-create", (), GPS),
    ("get", "zones", (), None),
    ("get", "clients", (), None),
    ("get", "plannings", (), None),
    ("get", "plannings_next", (), None),
    ("post", "plannings_abandon", (), {"planning_id": 1, "reason": "No puedo acceder"}),
    ("get", "movements", (), None),
    ("get", "identifiers", (), {"label": "MATRICULA", "asset_type": "CAMION"}),
    ("get", "config", (), {"key": "m3"}),
    ("get", "portes_planning", (), None),
    ("post", "porte_submit", (1,), {"action": "recogida"}),
    ("get", "asset-delivery-check", (1,), None),
    ("get", "machine_hours_list", (), None),
    ("get", "machine_hours_history", (), None),
    ("get", "laborer_zones", (), None),
    ("post", "laborer_transaction", (), None),
    ("get", "laborer_status", (), None),
    ("get", "planning_drafts", (), None),
    ("post", "planning_drafts", (), {"destination_text": "Obra en Alicante"}),
    ("get", "planning_draft_service_types", (), None),
    ("get", "planning_draft_detail", (1,), None),
    ("patch", "planning_draft_detail", (1,), {"notes": "changed"}),
    ("delete", "planning_draft_detail", (1,), None),
    ("post", "planning_draft_submit", (1,), None),
    ("get", "machines", (), None),
    ("get", "employees", (), None),
    ("get", "latest_transaction_document", (), None),
]

# Routes that are deliberately not behind JWT auth:
#   asset_qr           -- public QR image (authentication_classes = []).
#   transaction_imagen -- plain Django view from assets, not a DRF endpoint.
EXEMPT_ROUTE_NAMES = {"asset_qr", "transaction_imagen"}


class FleetEndpointsRequireAuthenticationTests(TestCase):
    def test_anonymous_request_gets_401(self):
        client = APIClient()
        for method, name, args, payload in ANONYMOUS_REQUESTS:
            with self.subTest(method=method.upper(), name=name):
                path = reverse(name, args=args)
                if method == "get":
                    response = client.get(path, data=payload)
                else:
                    response = getattr(client, method)(path, data=payload, format="json")
                self.assertEqual(response.status_code, 401)

    def test_every_fleet_route_is_listed(self):
        """A new endpoint must be added to ANONYMOUS_REQUESTS (or exempted on purpose)."""
        route_names = {pattern.name for pattern in fleet_urls.urlpatterns}
        listed = {name for _, name, _, _ in ANONYMOUS_REQUESTS}
        self.assertEqual(route_names - listed, EXEMPT_ROUTE_NAMES)
        self.assertEqual(listed - route_names, set())
