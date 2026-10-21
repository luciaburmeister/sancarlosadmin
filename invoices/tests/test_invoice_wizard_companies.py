"""The sales invoice wizard offers only active companies, and their clients."""
import json

from django.contrib.auth.models import User
from django.test import RequestFactory, TestCase
from django.urls import reverse

from invoices.forms.wizard_forms import SalesInvoiceBasicForm
from invoices.tests.factories import make_client, make_company


class WizardCompanyAndClientTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_superuser("wiz", "w@x.com", "pw")
        self.active = make_company(name="Activa SL")
        self.inactive = make_company(name="Cerrada SL", is_active=False)
        self.client_active = make_client(company=self.active, name="Cliente activo")
        self.client_inactive = make_client(company=self.inactive, name="Cliente cerrado")

    def form(self, data=None):
        request = RequestFactory().post("/") if data else RequestFactory().get("/")
        request.user = self.user
        return SalesInvoiceBasicForm(data=data, request=request, initial={})

    def data(self, company, client):
        return {
            "company": company.pk, "client": client.pk, "date_issued": "2026-10-21",
            "due_date": "2026-11-21", "payment_method": "transfer", "currency": "EUR",
        }

    def test_only_active_companies_are_offered(self):
        self.assertEqual(list(self.form().fields["company"].queryset), [self.active])

    def test_first_load_carries_no_clients(self):
        self.assertFalse(self.form().fields["client"].queryset.exists())

    def test_client_of_an_inactive_company_is_refused(self):
        form = self.form(self.data(self.inactive, self.client_inactive))

        self.assertFalse(form.is_valid())
        self.assertIn("company", form.errors)

    def test_client_must_belong_to_the_chosen_company(self):
        other = make_company(name="Otra SL")
        form = self.form(self.data(other, self.client_active))

        self.assertFalse(form.is_valid())
        self.assertIn("client", form.errors)

    def test_valid_selection(self):
        self.assertTrue(self.form(self.data(self.active, self.client_active)).is_valid())

    def test_autocomplete_hides_clients_of_inactive_companies(self):
        self.client.force_login(self.user)

        response = self.client.get(reverse("admin:client_autocomplete"), {"term": "Cliente"})

        names = {row["text"] for row in json.loads(response.content)["results"]}
        self.assertEqual(names, {"Cliente activo"})

    def test_wizard_page_loads(self):
        self.client.force_login(self.user)

        self.assertEqual(self.client.get(reverse("admin:invoices_salesinvoice_wizard")).status_code, 200)
