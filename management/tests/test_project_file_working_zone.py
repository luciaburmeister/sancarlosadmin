"""Tests for the ProjectFile <-> WorkingZone requirement and its per-company enforcement."""
from django.contrib import admin
from django.contrib.auth import get_user_model
from django.test import RequestFactory, TestCase
from django.urls import reverse

from company.models import Company, WorkingZone
from management.admin.project_file import ProjectFileWorkingZoneInline
from management.forms import LinkProjectFileForm
from management.models import ProjectFile
from sales.models import Client, Quote

User = get_user_model()


class ProjectFileWorkingZoneInlineMinNumTests(TestCase):
    """Unit tests for the dynamic min_num logic, without a full admin round trip."""

    def setUp(self):
        self.company = Company.objects.create(name="Taller Co")
        self.factory = RequestFactory()
        self.inline = ProjectFileWorkingZoneInline(ProjectFile, admin_site=admin.site)

    def _request(self, company_id=None):
        request = self.factory.post("/", data={"company": company_id} if company_id else {})
        return request

    def test_min_num_zero_when_company_does_not_require_it(self):
        request = self._request(company_id=self.company.pk)
        self.assertEqual(self.inline.get_min_num(request, obj=None), 0)

    def test_min_num_one_when_new_object_and_company_requires_it(self):
        self.company.require_working_zone_on_project_file = True
        self.company.save(update_fields=["require_working_zone_on_project_file"])
        request = self._request(company_id=self.company.pk)
        self.assertEqual(self.inline.get_min_num(request, obj=None), 1)

    def test_min_num_zero_when_no_company_in_request(self):
        request = self._request(company_id=None)
        self.assertEqual(self.inline.get_min_num(request, obj=None), 0)

    def test_min_num_zero_for_existing_object_even_if_company_requires_it(self):
        self.company.require_working_zone_on_project_file = True
        self.company.save(update_fields=["require_working_zone_on_project_file"])
        client_obj = Client.objects.create(name="Acme", company=self.company)
        pf = ProjectFile.objects.create(name="Histórico", company=self.company, client=client_obj)
        request = self._request(company_id=self.company.pk)
        self.assertEqual(self.inline.get_min_num(request, obj=pf), 0)


class LinkProjectFileFormWorkingZoneTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Taller Co")
        self.client_obj = Client.objects.create(name="Acme", company=self.company)
        self.zone = WorkingZone.objects.create(
            company=self.company,
            name="Obra Centro",
            location="POINT(-3.70379 40.416775)",
        )

    def _form(self, **overrides):
        data = {
            "action_type": "new",
            "new_project_name": "Reparación",
            **overrides,
        }
        return LinkProjectFileForm(data=data, company=self.company)

    def test_working_zones_optional_when_company_does_not_require_it(self):
        form = self._form()
        self.assertTrue(form.is_valid(), form.errors)

    def test_working_zones_required_when_company_requires_it(self):
        self.company.require_working_zone_on_project_file = True
        self.company.save(update_fields=["require_working_zone_on_project_file"])
        form = self._form()
        self.assertFalse(form.is_valid())
        self.assertIn("working_zones", form.errors)

    def test_working_zones_requirement_satisfied(self):
        self.company.require_working_zone_on_project_file = True
        self.company.save(update_fields=["require_working_zone_on_project_file"])
        form = self._form(working_zones=[self.zone.pk])
        self.assertTrue(form.is_valid(), form.errors)

    def test_existing_action_never_blocked_by_requirement(self):
        self.company.require_working_zone_on_project_file = True
        self.company.save(update_fields=["require_working_zone_on_project_file"])
        pf = ProjectFile.objects.create(name="Histórico", company=self.company, client=self.client_obj)
        form = self._form(action_type="existing", existing_project=pf.pk)
        self.assertTrue(form.is_valid(), form.errors)

    def test_zone_already_linked_to_another_project_is_not_offered(self):
        other_pf = ProjectFile.objects.create(name="Otro", company=self.company, client=self.client_obj)
        self.zone.project_file = other_pf
        self.zone.save(update_fields=["project_file"])
        form = self._form()
        self.assertNotIn(self.zone, form.fields["working_zones"].queryset)


class QuoteAcceptanceWizardWorkingZoneTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Taller Co")
        self.client_obj = Client.objects.create(name="Acme", company=self.company)
        self.zone = WorkingZone.objects.create(
            company=self.company,
            name="Obra Centro",
            location="POINT(-3.70379 40.416775)",
        )
        self.quote = Quote.objects.create(
            company=self.company, client=self.client_obj, project_name="Proyecto X",
        )
        self.user = User.objects.create_superuser(
            username="admin", email="admin@test.com", password="pass1234",
        )
        self.client.force_login(self.user)
        self.url = reverse("admin:sales_quote_acceptance_wizard", args=[self.quote.id])

    def test_new_expediente_blocked_without_zone_when_required(self):
        self.company.require_working_zone_on_project_file = True
        self.company.save(update_fields=["require_working_zone_on_project_file"])
        response = self.client.post(self.url, data={
            "action_type": "new",
            "new_project_name": "Proyecto X",
        })
        self.assertEqual(response.status_code, 200)  # re-rendered with errors, no redirect
        self.assertFalse(ProjectFile.objects.exists())

    def test_new_expediente_succeeds_and_links_zone_when_required(self):
        self.company.require_working_zone_on_project_file = True
        self.company.save(update_fields=["require_working_zone_on_project_file"])
        response = self.client.post(self.url, data={
            "action_type": "new",
            "new_project_name": "Proyecto X",
            "working_zones": [self.zone.pk],
        })
        self.assertEqual(response.status_code, 302)
        project_file = ProjectFile.objects.get()
        self.zone.refresh_from_db()
        self.assertEqual(self.zone.project_file, project_file)

    def test_existing_action_unaffected_by_requirement(self):
        self.company.require_working_zone_on_project_file = True
        self.company.save(update_fields=["require_working_zone_on_project_file"])
        existing_pf = ProjectFile.objects.create(
            name="Histórico", company=self.company, client=self.client_obj,
        )
        response = self.client.post(self.url, data={
            "action_type": "existing",
            "existing_project": existing_pf.pk,
        })
        self.assertEqual(response.status_code, 302)
