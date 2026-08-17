"""
api/fleet/tests/test_planning_drafts.py

GET /es/api/machines/ — regression coverage for the bug where the endpoint
filtered by client_id and therefore always returned an empty list, since the
company's own machines (excavators, loaders, ...) are never assigned an
owning client.

Run just this file:
    python manage.py test api.fleet.tests.test_planning_drafts
"""
from rest_framework.test import APIClient

from django.test import TestCase

from assets.models import (
    Asset, AssetPlanningRequestDraft, AssetPlanningRequestDraftStatus,
    AssetType, AssetTypeCategory, ServiceType,
)
from api.fleet.tests.helpers import make_company, make_employee, make_user, make_zone
from sales.models import Client


def _make_asset(company, category, name, is_active=True, client=None):
    asset_type = AssetType.objects.create(company=company, name=f"{name} type", category=category)
    return Asset.objects.create(
        company=company, asset_type=asset_type, name=name, is_active=is_active, client=client,
    )


class MachinesListEndpointTests(TestCase):
    def setUp(self):
        self.client_api = APIClient()
        self.company = make_company()
        self.user = make_user()
        self.employee = make_employee(self.company, self.user)
        self.client_api.force_authenticate(user=self.user)

    def _url(self):
        return "/es/api/machines/"

    def test_unauthenticated_request_is_rejected(self):
        self.client_api.force_authenticate(user=None)
        response = self.client_api.get(self._url())
        self.assertEqual(response.status_code, 401)

    def test_returns_company_machine_with_no_owning_client(self):
        # This is the exact shape of real data: the company's own machines
        # are never assigned to a client. The endpoint must not require one.
        machine = _make_asset(self.company, AssetTypeCategory.MACHINE, "KUBOTA U 17-3", client=None)
        response = self.client_api.get(self._url())
        self.assertEqual(response.status_code, 200)
        ids = [r["id"] for r in response.json()["results"]]
        self.assertIn(machine.pk, ids)

    def test_excludes_trucks(self):
        _make_asset(self.company, AssetTypeCategory.TRUCK, "Truck 1")
        response = self.client_api.get(self._url())
        names = [r["name"] for r in response.json()["results"]]
        self.assertNotIn("Truck 1", names)

    def test_excludes_inactive_machines(self):
        _make_asset(self.company, AssetTypeCategory.MACHINE, "Retired excavator", is_active=False)
        response = self.client_api.get(self._url())
        names = [r["name"] for r in response.json()["results"]]
        self.assertNotIn("Retired excavator", names)

    def test_excludes_other_companies_machines(self):
        other_company = make_company(name="Other Company")
        _make_asset(other_company, AssetTypeCategory.MACHINE, "Other Co Machine")
        response = self.client_api.get(self._url())
        names = [r["name"] for r in response.json()["results"]]
        self.assertNotIn("Other Co Machine", names)

    def test_client_query_param_is_ignored_not_required(self):
        # The list used to be (wrongly) scoped by ?client=<id>. Any value —
        # or none at all — must return the same company-wide list now.
        machine = _make_asset(self.company, AssetTypeCategory.MACHINE, "KUBOTA U 17-3")
        response = self.client_api.get(self._url(), {"client": 999999})
        ids = [r["id"] for r in response.json()["results"]]
        self.assertIn(machine.pk, ids)


class PlanningDraftDraftCreateProcessedFlowTests(TestCase):
    """AssetPlanningRequestDraft lifecycle exercised through the mobile API."""

    def setUp(self):
        self.client_api = APIClient()
        self.company = make_company()
        self.user = make_user()
        self.employee = make_employee(self.company, self.user)
        self.client_api.force_authenticate(user=self.user)
        self.sales_client = Client.objects.create(name="Client 1", company=self.company)

    def test_create_draft_defaults_to_draft_status(self):
        response = self.client_api.post(
            "/es/api/planning-drafts/",
            {
                "service_type": ServiceType.SERVICIO_GRUA,
                "client": self.sales_client.pk,
                "destination_text": "Obra en Alicante",
                "needed_date": "2026-09-01",
            },
        )
        self.assertEqual(response.status_code, 201)
        draft = AssetPlanningRequestDraft.objects.get(pk=response.json()["id"])
        self.assertEqual(draft.status, AssetPlanningRequestDraftStatus.DRAFT)
        self.assertEqual(draft.requested_by_id, self.employee.pk)
        self.assertEqual(draft.company_id, self.company.pk)

    def test_submitted_draft_cannot_be_edited(self):
        draft = AssetPlanningRequestDraft.objects.create(
            company=self.company,
            requested_by=self.employee,
            service_type=ServiceType.SERVICIO_GRUA,
            client=self.sales_client,
            destination_text="Obra en Alicante",
            status=AssetPlanningRequestDraftStatus.SUBMITTED,
            needed_date="2026-09-01",
        )
        response = self.client_api.patch(
            f"/es/api/planning-drafts/{draft.pk}/", {"notes": "changed"}, format="json",
        )
        self.assertEqual(response.status_code, 400)

    def test_missing_fields_block_submit(self):
        draft = AssetPlanningRequestDraft.objects.create(
            company=self.company,
            requested_by=self.employee,
            service_type=ServiceType.SERVICIO_GRUA,
            client=self.sales_client,
            destination_text="",
            needed_date="2026-09-01",
        )
        response = self.client_api.post(f"/es/api/planning-drafts/{draft.pk}/submit/")
        self.assertEqual(response.status_code, 400)
        self.assertIn("destination_text", response.json()["missing_fields"])

    def test_needed_time_and_urgency_are_optional_and_roundtrip(self):
        response = self.client_api.post(
            "/es/api/planning-drafts/",
            {
                "service_type": ServiceType.SERVICIO_GRUA,
                "client": self.sales_client.pk,
                "destination_text": "Obra en Alicante",
                "needed_date": "2026-09-01",
            },
        )
        self.assertEqual(response.status_code, 201)
        draft = AssetPlanningRequestDraft.objects.get(pk=response.json()["id"])
        self.assertIsNone(draft.needed_time)
        self.assertEqual(draft.urgency, "")
        self.assertEqual(draft.missing_fields_for_submit(), [])

        response = self.client_api.patch(
            f"/es/api/planning-drafts/{draft.pk}/",
            {"needed_time": "08:30:00", "urgency": "urgent"},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        draft.refresh_from_db()
        self.assertEqual(str(draft.needed_time), "08:30:00")
        self.assertEqual(draft.urgency, "urgent")

        # Clearing them back (the app always sends these keys, using "" for
        # "not set" — this must not blow up on TimeField.to_python("")).
        response = self.client_api.patch(
            f"/es/api/planning-drafts/{draft.pk}/",
            {"needed_time": "", "urgency": ""},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        draft.refresh_from_db()
        self.assertIsNone(draft.needed_time)
        self.assertEqual(draft.urgency, "")

    def test_empty_string_needed_time_on_create_does_not_error(self):
        # Regression: the app always sends needed_time/urgency, using "" when
        # the employee left them unset. An empty string used to reach
        # TimeField.to_python() unchanged and raise a ValidationError on save.
        response = self.client_api.post(
            "/es/api/planning-drafts/",
            {
                "service_type": ServiceType.SERVICIO_GRUA,
                "client": self.sales_client.pk,
                "destination_text": "Obra en Alicante",
                "needed_date": "2026-09-01",
                "needed_time": "",
                "urgency": "",
            },
        )
        self.assertEqual(response.status_code, 201)
        draft = AssetPlanningRequestDraft.objects.get(pk=response.json()["id"])
        self.assertIsNone(draft.needed_time)
        self.assertEqual(draft.urgency, "")


class NewClientRequestTests(TestCase):
    """Requesting a not-yet-existing client instead of picking a real one."""

    def setUp(self):
        self.client_api = APIClient()
        self.company = make_company()
        self.user = make_user()
        self.employee = make_employee(self.company, self.user)
        self.client_api.force_authenticate(user=self.user)
        self.sales_client = Client.objects.create(name="Client 1", company=self.company)

    def test_create_with_new_client_name_only(self):
        response = self.client_api.post(
            "/es/api/planning-drafts/",
            {
                "service_type": ServiceType.SERVICIO_GRUA,
                "new_client_name": "Constructora Nueva SL",
                "new_client_tax_id": "B12345678",
                "new_client_fiscal_address": "Calle Falsa 123, Madrid",
                "destination_text": "Obra en Alicante",
                "needed_date": "2026-09-01",
            },
        )
        self.assertEqual(response.status_code, 201)
        draft = AssetPlanningRequestDraft.objects.get(pk=response.json()["id"])
        self.assertIsNone(draft.client_id)
        self.assertTrue(draft.requests_new_client)
        self.assertEqual(draft.new_client_name, "Constructora Nueva SL")
        self.assertEqual(draft.new_client_tax_id, "B12345678")
        self.assertEqual(draft.new_client_fiscal_address, "Calle Falsa 123, Madrid")
        self.assertEqual(draft.missing_fields_for_submit(), [])

        # Also check the API response body carries them back — this is what
        # the app and the admin actually read.
        body = response.json()
        self.assertEqual(body["new_client_tax_id"], "B12345678")
        self.assertEqual(body["new_client_fiscal_address"], "Calle Falsa 123, Madrid")

    def test_create_without_client_or_new_client_name_fails(self):
        response = self.client_api.post(
            "/es/api/planning-drafts/",
            {
                "service_type": ServiceType.SERVICIO_GRUA,
                "destination_text": "Obra en Alicante",
                "needed_date": "2026-09-01",
            },
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(AssetPlanningRequestDraft.objects.count(), 0)

    def test_missing_fields_for_submit_does_not_require_a_real_client(self):
        draft = AssetPlanningRequestDraft.objects.create(
            company=self.company,
            requested_by=self.employee,
            service_type=ServiceType.SERVICIO_GRUA,
            new_client_name="Constructora Nueva SL",
            destination_text="Obra en Alicante",
            needed_date="2026-09-01",
        )
        self.assertNotIn("client", draft.missing_fields_for_submit())

    def test_missing_fields_for_submit_requires_client_or_new_client_name(self):
        draft = AssetPlanningRequestDraft.objects.create(
            company=self.company,
            requested_by=self.employee,
            service_type=ServiceType.SERVICIO_GRUA,
            destination_text="Obra en Alicante",
            needed_date="2026-09-01",
        )
        self.assertIn("client", draft.missing_fields_for_submit())

    def test_patching_new_client_name_clears_a_previously_set_client(self):
        draft = AssetPlanningRequestDraft.objects.create(
            company=self.company,
            requested_by=self.employee,
            service_type=ServiceType.SERVICIO_GRUA,
            client=self.sales_client,
            destination_text="Obra en Alicante",
            needed_date="2026-09-01",
        )
        response = self.client_api.patch(
            f"/es/api/planning-drafts/{draft.pk}/",
            {"new_client_name": "Constructora Nueva SL"},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        draft.refresh_from_db()
        self.assertIsNone(draft.client_id)
        self.assertEqual(draft.new_client_name, "Constructora Nueva SL")

    def test_patching_client_clears_new_client_fields(self):
        draft = AssetPlanningRequestDraft.objects.create(
            company=self.company,
            requested_by=self.employee,
            service_type=ServiceType.SERVICIO_GRUA,
            new_client_name="Constructora Nueva SL",
            new_client_tax_id="B12345678",
            destination_text="Obra en Alicante",
            needed_date="2026-09-01",
        )
        response = self.client_api.patch(
            f"/es/api/planning-drafts/{draft.pk}/",
            {"client": self.sales_client.pk},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        draft.refresh_from_db()
        self.assertEqual(draft.client_id, self.sales_client.pk)
        self.assertEqual(draft.new_client_name, "")
        self.assertEqual(draft.new_client_tax_id, "")


class NewZoneRequestTests(TestCase):
    """Requesting a not-yet-existing working zone instead of picking a real one."""

    def setUp(self):
        self.client_api = APIClient()
        self.company = make_company()
        self.user = make_user()
        self.employee = make_employee(self.company, self.user)
        self.client_api.force_authenticate(user=self.user)
        self.sales_client = Client.objects.create(name="Client 1", company=self.company)
        self.zone = make_zone(self.company, name="Obra Centro")

    def test_create_with_new_destination_zone_only(self):
        response = self.client_api.post(
            "/es/api/planning-drafts/",
            {
                "service_type": ServiceType.SERVICIO_GRUA,
                "client": self.sales_client.pk,
                "destination_text": "Calle Nueva 5",
                "destination_city": "Marbella",
                "needed_date": "2026-09-01",
            },
        )
        self.assertEqual(response.status_code, 201)
        draft = AssetPlanningRequestDraft.objects.get(pk=response.json()["id"])
        self.assertIsNone(draft.to_zone_id)
        self.assertTrue(draft.requests_new_destination_zone)
        self.assertEqual(draft.destination_text, "Calle Nueva 5")
        self.assertEqual(draft.destination_city, "Marbella")
        self.assertEqual(draft.missing_fields_for_submit(), [])

    def test_destination_city_defaults_to_malaga(self):
        draft = AssetPlanningRequestDraft.objects.create(
            company=self.company,
            requested_by=self.employee,
            service_type=ServiceType.SERVICIO_GRUA,
            client=self.sales_client,
            needed_date="2026-09-01",
        )
        self.assertEqual(draft.destination_city, "Málaga")
        self.assertEqual(draft.origin_city, "Málaga")

    def test_missing_fields_requires_destination_zone_or_text(self):
        draft = AssetPlanningRequestDraft.objects.create(
            company=self.company,
            requested_by=self.employee,
            service_type=ServiceType.SERVICIO_GRUA,
            client=self.sales_client,
            needed_date="2026-09-01",
        )
        self.assertIn("destination_text", draft.missing_fields_for_submit())

        draft.to_zone = self.zone
        draft.save()
        self.assertNotIn("destination_text", draft.missing_fields_for_submit())

    def test_origin_only_required_for_service_types_that_need_it(self):
        # SERVICIO_GRUA doesn't need from_zone at all.
        draft = AssetPlanningRequestDraft.objects.create(
            company=self.company,
            requested_by=self.employee,
            service_type=ServiceType.SERVICIO_GRUA,
            client=self.sales_client,
            to_zone=self.zone,
            needed_date="2026-09-01",
        )
        self.assertNotIn("origin_text", draft.missing_fields_for_submit())

        # PORTE_MAQUINA_PROPIA does need from_zone.
        draft.service_type = ServiceType.PORTE_MAQUINA_PROPIA
        draft.save()
        self.assertIn("origin_text", draft.missing_fields_for_submit())

    def test_patching_to_zone_clears_destination_text(self):
        draft = AssetPlanningRequestDraft.objects.create(
            company=self.company,
            requested_by=self.employee,
            service_type=ServiceType.SERVICIO_GRUA,
            client=self.sales_client,
            destination_text="Calle Provisional 1",
            needed_date="2026-09-01",
        )
        response = self.client_api.patch(
            f"/es/api/planning-drafts/{draft.pk}/",
            {"to_zone": self.zone.pk},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        draft.refresh_from_db()
        self.assertEqual(draft.to_zone_id, self.zone.pk)
        self.assertEqual(draft.destination_text, "")

    def test_patching_destination_text_clears_to_zone(self):
        draft = AssetPlanningRequestDraft.objects.create(
            company=self.company,
            requested_by=self.employee,
            service_type=ServiceType.SERVICIO_GRUA,
            client=self.sales_client,
            to_zone=self.zone,
            needed_date="2026-09-01",
        )
        response = self.client_api.patch(
            f"/es/api/planning-drafts/{draft.pk}/",
            {"destination_text": "Calle Nueva 9", "destination_city": "Marbella"},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        draft.refresh_from_db()
        self.assertIsNone(draft.to_zone_id)
        self.assertEqual(draft.destination_text, "Calle Nueva 9")


class CancelRequestTests(TestCase):
    """DELETE /planning-drafts/<id>/ — cancellable up until a planner processes it."""

    def setUp(self):
        self.client_api = APIClient()
        self.company = make_company()
        self.user = make_user()
        self.employee = make_employee(self.company, self.user)
        self.client_api.force_authenticate(user=self.user)
        self.sales_client = Client.objects.create(name="Client 1", company=self.company)

    def _make_draft(self, status):
        return AssetPlanningRequestDraft.objects.create(
            company=self.company,
            requested_by=self.employee,
            service_type=ServiceType.SERVICIO_GRUA,
            client=self.sales_client,
            destination_text="Obra en Alicante",
            status=status,
            needed_date="2026-09-01",
        )

    def test_can_cancel_a_submitted_request(self):
        draft = self._make_draft(AssetPlanningRequestDraftStatus.SUBMITTED)
        response = self.client_api.delete(f"/es/api/planning-drafts/{draft.pk}/")
        self.assertEqual(response.status_code, 200)
        draft.refresh_from_db()
        self.assertEqual(draft.status, AssetPlanningRequestDraftStatus.CANCELLED)

    def test_can_still_cancel_a_draft(self):
        draft = self._make_draft(AssetPlanningRequestDraftStatus.DRAFT)
        response = self.client_api.delete(f"/es/api/planning-drafts/{draft.pk}/")
        self.assertEqual(response.status_code, 200)
        draft.refresh_from_db()
        self.assertEqual(draft.status, AssetPlanningRequestDraftStatus.CANCELLED)

    def test_cannot_cancel_a_processed_request(self):
        draft = self._make_draft(AssetPlanningRequestDraftStatus.PROCESSED)
        response = self.client_api.delete(f"/es/api/planning-drafts/{draft.pk}/")
        self.assertEqual(response.status_code, 400)
        draft.refresh_from_db()
        self.assertEqual(draft.status, AssetPlanningRequestDraftStatus.PROCESSED)

    def test_cannot_cancel_an_already_cancelled_request(self):
        draft = self._make_draft(AssetPlanningRequestDraftStatus.CANCELLED)
        response = self.client_api.delete(f"/es/api/planning-drafts/{draft.pk}/")
        self.assertEqual(response.status_code, 400)
