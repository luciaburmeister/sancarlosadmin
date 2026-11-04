"""/api/v1/clients/ and /api/v1/tax-types/."""
from django.test import TestCase
from rest_framework.test import APIClient

from apps.public_api.tests.factories import auth_header, make_api_key, make_api_user
from invoices.models import TaxType
from invoices.tests.factories import get_irpf, get_iva21, make_client, make_company


class _Base(TestCase):
    perms = ()

    @classmethod
    def setUpTestData(cls):
        cls.company = make_company()
        cls.other_company = make_company(name="Other Co")
        cls.user = make_api_user(company=cls.company, perms=cls.perms)

    def setUp(self):
        self.api = APIClient()
        _, secret = make_api_key(user=self.user, company=self.company)
        self.headers = auth_header(secret)

    def get(self, url, **params):
        return self.api.get(url, params, **self.headers)


class ClientTests(_Base):
    perms = ("sales.view_client",)

    def test_lists_only_the_key_company_by_name(self):
        zeta = make_client(company=self.company, name="Zeta")
        acme = make_client(company=self.company, name="Acme")
        make_client(company=self.other_company, name="Foreign")

        body = self.get("/api/v1/clients/").json()

        self.assertEqual([row["id"] for row in body["results"]], [acme.pk, zeta.pk])
        self.assertEqual(
            set(body["results"][0]),
            {
                "id", "name", "commercial_name", "tax_identifier", "email", "phone", "address", "language",
                "payment_method", "payment_terms_days", "payment_days", "created_at", "updated_at",
            },
        )

    def test_search_and_exact_tax_identifier(self):
        acme = make_client(company=self.company, name="Acme")
        acme.tax_identifier = "B12345678"
        acme.save()
        make_client(company=self.company, name="Other")

        def ids(**params):
            return [row["id"] for row in self.get("/api/v1/clients/", **params).json()["results"]]

        self.assertEqual(ids(search="acm"), [acme.pk])
        self.assertEqual(ids(tax_identifier="b12345678"), [acme.pk])
        self.assertEqual(ids(tax_identifier="B1234"), [])

    def test_detail_and_other_company_is_404(self):
        acme = make_client(company=self.company, name="Acme")
        foreign = make_client(company=self.other_company, name="Foreign")
        self.assertEqual(self.get(f"/api/v1/clients/{acme.pk}/").json()["name"], "Acme")
        self.assertEqual(self.get(f"/api/v1/clients/{foreign.pk}/").status_code, 404)


class ClientPermissionTests(_Base):
    def test_needs_view_client(self):
        self.assertEqual(self.get("/api/v1/clients/").status_code, 403)


class TaxTypeTests(_Base):
    perms = ("invoices.view_taxtype",)

    def test_lists_active_tax_types_with_their_rates(self):
        iva = get_iva21()
        iva.percentage_options = [21, 10, 4, 0]
        iva.save()
        irpf = get_irpf()
        TaxType.objects.create(tax_name="Old", country="ES", is_active=False)
        TaxType.objects.create(tax_name="VAT", country="PT", percentage_options=[23])

        body = self.get("/api/v1/tax-types/", country="es").json()

        self.assertEqual(
            body["results"],
            [
                {"id": irpf.pk, "name": "IRPF", "country": "ES", "behavior": "subtract", "rates": [], "default_rate": "0.00"},
                {
                    "id": iva.pk, "name": "IVA", "country": "ES", "behavior": "add",
                    "rates": ["0.00", "4.00", "10.00", "21.00"], "default_rate": "0.00",
                },
            ],
        )
        self.assertEqual(self.get("/api/v1/tax-types/").json()["count"], 3)

    def test_needs_view_taxtype(self):
        user = make_api_user(company=self.company, username="noperm")
        _, secret = make_api_key(user=user, company=self.company)
        self.assertEqual(self.api.get("/api/v1/tax-types/", **auth_header(secret)).status_code, 403)
