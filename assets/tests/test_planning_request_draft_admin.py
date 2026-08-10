"""
assets/tests/test_planning_request_draft_admin.py

Covers the draft -> request conversion flow added to
AssetPlanningRequestAdmin/AssetPlanningRequestDraftAdmin:
  - "Create real request" only redirects for SUBMITTED drafts.
  - The add/change form shows a read-only panel with the source draft's
    data (source_draft_panel.html) instead of the old behaviour of dumping
    it into the notes field.
  - Saving the request marks the draft PROCESSED and links resulting_request.
  - The Unfold TABS config (assets/admin/tabs.py) links Requests <-> Drafts
    with a live, company-scoped count badge on the Drafts tab.

Run:
    python manage.py test assets.tests.test_planning_request_draft_admin
"""
from datetime import timedelta

from django.contrib.admin.sites import AdminSite
from django.contrib.auth import get_user_model
from django.contrib.messages.middleware import MessageMiddleware
from django.contrib.sessions.middleware import SessionMiddleware
from django.test import RequestFactory, TestCase
from django.urls import reverse
from django.utils import translation

from assets.admin.asset_planning_request import AssetPlanningRequestAdmin
from assets.admin.tabs import asset_planning_tabs
from assets.models import (
    AssetPlanningRequest,
    AssetPlanningRequestDraft,
    AssetPlanningRequestDraftStatus,
    ServiceType,
)
from company.models import CompanyUser
from management.models import ProjectFile
from sales.models import Client
from api.fleet.tests.helpers import make_company, make_employee, make_user, make_zone

User = get_user_model()


class DraftAdminFixtureMixin:
    def setUp(self):
        super().setUp()
        self.company = make_company()
        self.to_zone = make_zone(self.company, name="To Zone")
        self.sales_client = Client.objects.create(name="Client 1", company=self.company)
        self.project_file = ProjectFile.objects.create(
            name="Project 1", client=self.sales_client, company=self.company,
        )
        self.superuser = User.objects.create_superuser(username="admin", password="x", email="a@a.com")
        make_employee(self.company, self.superuser)

    def _make_draft(self, status=AssetPlanningRequestDraftStatus.SUBMITTED, **overrides):
        fields = dict(
            company=self.company,
            service_type=ServiceType.SERVICIO_GRUA,
            client=self.sales_client,
            destination_text="Obra de prueba en Alicante",
            needed_date="2026-09-01",
            notes="Nota del empleado",
            status=status,
        )
        fields.update(overrides)
        return AssetPlanningRequestDraft.objects.create(**fields)


class CreateRequestActionTests(DraftAdminFixtureMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.superuser)

    def test_redirects_to_prefilled_add_form_for_submitted_draft(self):
        draft = self._make_draft(status=AssetPlanningRequestDraftStatus.SUBMITTED)
        url = reverse("admin:assets_assetplanningrequestdraft_create_request_action", args=[draft.pk])
        response = self.client.get(url)
        expected = reverse("admin:assets_assetplanningrequest_add") + f"?draft_id={draft.pk}"
        self.assertRedirects(response, expected, fetch_redirect_response=False)

    def test_rejects_draft_still_in_draft_status(self):
        draft = self._make_draft(status=AssetPlanningRequestDraftStatus.DRAFT)
        url = reverse("admin:assets_assetplanningrequestdraft_create_request_action", args=[draft.pk])
        response = self.client.get(url)
        self.assertRedirects(response, reverse("admin:assets_assetplanningrequestdraft_changelist"))
        self.assertEqual(AssetPlanningRequestDraft.objects.get(pk=draft.pk).status, AssetPlanningRequestDraftStatus.DRAFT)


class AddViewDraftPanelTests(DraftAdminFixtureMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.superuser)

    def test_add_with_draft_id_prefills_form_and_shows_panel(self):
        draft = self._make_draft()
        url = reverse("admin:assets_assetplanningrequest_add") + f"?draft_id={draft.pk}"
        response = self.client.get(url)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Del borrador móvil")
        self.assertContains(response, draft.destination_text)

        initial = response.context["adminform"].form.initial
        self.assertEqual(initial["service_type"], draft.service_type)
        self.assertEqual(initial["notes"], draft.notes)

    def test_notes_initial_is_not_polluted_with_draft_summary(self):
        # Regression: notes used to be overwritten with a
        # "[Borrador móvil de ...]" text blob mixing the draft's hints into
        # the reviewer's own notes field.
        draft = self._make_draft(notes="Nota original del empleado")
        url = reverse("admin:assets_assetplanningrequest_add") + f"?draft_id={draft.pk}"
        response = self.client.get(url)
        initial = response.context["adminform"].form.initial
        self.assertEqual(initial["notes"], "Nota original del empleado")
        self.assertNotIn("Borrador móvil de", initial["notes"])

    def test_add_without_draft_id_has_no_panel(self):
        response = self.client.get(reverse("admin:assets_assetplanningrequest_add"))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "Del borrador móvil")

    def test_add_with_unknown_draft_id_is_ignored(self):
        response = self.client.get(
            reverse("admin:assets_assetplanningrequest_add") + "?draft_id=999999"
        )
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "Del borrador móvil")


class ChangeViewDraftPanelTests(DraftAdminFixtureMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.superuser)
        self.request_obj = AssetPlanningRequest.objects.create(
            company=self.company,
            service_type=ServiceType.SERVICIO_GRUA,
            to_zone=self.to_zone,
            needed_date="2026-09-01",
            project_file=self.project_file,
            estimated_duration=timedelta(hours=1),
        )

    def test_change_view_shows_panel_when_linked_to_a_draft(self):
        draft = self._make_draft(
            status=AssetPlanningRequestDraftStatus.PROCESSED,
            resulting_request=self.request_obj,
        )
        response = self.client.get(
            reverse("admin:assets_assetplanningrequest_change", args=[self.request_obj.pk])
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Del borrador móvil")
        self.assertContains(response, draft.destination_text)

    def test_change_view_has_no_panel_without_a_linked_draft(self):
        response = self.client.get(
            reverse("admin:assets_assetplanningrequest_change", args=[self.request_obj.pk])
        )
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "Del borrador móvil")


class ResponseAddProcessesDraftTests(DraftAdminFixtureMixin, TestCase):
    def _request_with_draft_id(self, draft_id):
        req = RequestFactory().post(f"/?draft_id={draft_id}")
        SessionMiddleware(lambda r: None).process_request(req)
        req.session.save()
        MessageMiddleware(lambda r: None).process_request(req)
        req.user = self.superuser
        return req

    def test_saving_the_request_marks_the_draft_processed_and_links_it(self):
        draft = self._make_draft()
        obj = AssetPlanningRequest.objects.create(
            company=self.company,
            service_type=ServiceType.SERVICIO_GRUA,
            to_zone=self.to_zone,
            needed_date="2026-09-01",
            project_file=self.project_file,
            estimated_duration=timedelta(hours=1),
        )
        admin_instance = AssetPlanningRequestAdmin(AssetPlanningRequest, admin_site=AdminSite())
        req = self._request_with_draft_id(draft.pk)
        admin_instance.response_add(req, obj)

        draft.refresh_from_db()
        self.assertEqual(draft.status, AssetPlanningRequestDraftStatus.PROCESSED)
        self.assertEqual(draft.resulting_request_id, obj.pk)

    def test_saving_without_a_draft_id_leaves_other_drafts_untouched(self):
        draft = self._make_draft()
        obj = AssetPlanningRequest.objects.create(
            company=self.company,
            service_type=ServiceType.SERVICIO_GRUA,
            to_zone=self.to_zone,
            needed_date="2026-09-01",
            project_file=self.project_file,
            estimated_duration=timedelta(hours=1),
        )
        admin_instance = AssetPlanningRequestAdmin(AssetPlanningRequest, admin_site=AdminSite())
        req = RequestFactory().post("/")
        SessionMiddleware(lambda r: None).process_request(req)
        req.session.save()
        MessageMiddleware(lambda r: None).process_request(req)
        req.user = self.superuser
        admin_instance.response_add(req, obj)

        draft.refresh_from_db()
        self.assertEqual(draft.status, AssetPlanningRequestDraftStatus.SUBMITTED)
        self.assertIsNone(draft.resulting_request_id)


class AssetPlanningTabsTests(DraftAdminFixtureMixin, TestCase):
    def _request_for(self, user):
        req = RequestFactory().get("/")
        req.user = user
        return req

    def test_returns_requests_and_drafts_links(self):
        with translation.override("en"):
            tabs = asset_planning_tabs(self._request_for(self.superuser))
        self.assertEqual(len(tabs), 1)
        items = tabs[0]["items"]
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]["title"], "Requests")
        self.assertEqual(items[0]["link"], reverse("admin:assets_assetplanningrequest_changelist"))
        self.assertEqual(items[1]["link"], reverse("admin:assets_assetplanningrequestdraft_changelist"))

    def test_drafts_tab_has_no_badge_when_nothing_submitted(self):
        with translation.override("en"):
            tabs = asset_planning_tabs(self._request_for(self.superuser))
        self.assertEqual(str(tabs[0]["items"][1]["title"]), "Drafts")

    def test_drafts_tab_badge_counts_only_submitted_drafts(self):
        self._make_draft(status=AssetPlanningRequestDraftStatus.SUBMITTED)
        self._make_draft(status=AssetPlanningRequestDraftStatus.SUBMITTED)
        self._make_draft(status=AssetPlanningRequestDraftStatus.DRAFT)  # not submitted, excluded
        self._make_draft(status=AssetPlanningRequestDraftStatus.PROCESSED)  # already handled, excluded

        with translation.override("en"):
            tabs = asset_planning_tabs(self._request_for(self.superuser))
        self.assertIn("2", str(tabs[0]["items"][1]["title"]))

    def test_drafts_tab_badge_is_scoped_to_the_users_companies(self):
        other_company = make_company(name="Other Company")
        other_user = make_user(username="other")
        CompanyUser.objects.create(user=other_user, company=other_company, is_active=True)

        # Two SUBMITTED drafts for self.company, one for other_company.
        self._make_draft(status=AssetPlanningRequestDraftStatus.SUBMITTED)
        self._make_draft(status=AssetPlanningRequestDraftStatus.SUBMITTED)
        AssetPlanningRequestDraft.objects.create(
            company=other_company,
            service_type=ServiceType.SERVICIO_GRUA,
            client=Client.objects.create(name="Other Client", company=other_company),
            destination_text="Elsewhere",
            needed_date="2026-09-01",
            status=AssetPlanningRequestDraftStatus.SUBMITTED,
        )

        # A non-superuser scoped only to other_company sees only its own draft.
        with translation.override("en"):
            other_tabs = asset_planning_tabs(self._request_for(other_user))
        self.assertIn("1", str(other_tabs[0]["items"][1]["title"]))

        # The superuser sees every company's drafts (2 + 1 = 3).
        with translation.override("en"):
            superuser_tabs = asset_planning_tabs(self._request_for(self.superuser))
        self.assertIn("3", str(superuser_tabs[0]["items"][1]["title"]))


class NewZoneRequestAdminTests(DraftAdminFixtureMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.superuser)

    def test_add_view_shows_new_zone_banner_and_carries_real_zone_forward(self):
        # Default fixture draft has destination_text but no to_zone.
        draft = self._make_draft(destination_text="Calle Nueva 5", destination_city="Marbella")
        url = reverse("admin:assets_assetplanningrequest_add") + f"?draft_id={draft.pk}"
        response = self.client.get(url)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "ZONA DE TRABAJO NUEVA SOLICITADA")
        self.assertContains(response, "Calle Nueva 5")
        self.assertContains(response, "Marbella")

    def test_add_view_prefills_real_to_zone_when_draft_has_one(self):
        draft = self._make_draft(to_zone=self.to_zone, destination_text="")
        url = reverse("admin:assets_assetplanningrequest_add") + f"?draft_id={draft.pk}"
        response = self.client.get(url)

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "ZONA DE TRABAJO NUEVA SOLICITADA")
        initial = response.context["adminform"].form.initial
        self.assertEqual(initial["to_zone"], self.to_zone.pk)

    def test_changelist_show_destination_marks_new_zone_requests(self):
        self._make_draft(destination_text="Calle Nueva 5", destination_city="Marbella")
        response = self.client.get(reverse("admin:assets_assetplanningrequestdraft_changelist"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Calle Nueva 5, Marbella")
