from datetime import date

from django.contrib.auth.models import User
from django.test import TestCase
from rest_framework.test import APIClient

from company.models import Company, Employee
from sales.models import Client


class ClientSearchTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user("tester", password="pass")
        cls.company = Company.objects.create(name="Test Co")
        Employee.objects.create(
            user=cls.user,
            company=cls.company,
            first_name="Test",
            last_name="User",
            is_active=True,
            hired_at=date(2020, 1, 1),
        )
        cls.match = Client.objects.create(
            name="Taller Norte",
            commercial_name="Norte",
            tax_identifier="B12345678",
            company=cls.company,
        )
        Client.objects.create(
            name="Taller Sur",
            tax_identifier="A99999999",
            company=cls.company,
        )
        other = Company.objects.create(name="Other Co")
        Client.objects.create(
            name="Ajeno",
            tax_identifier="B12345678",
            company=other,
        )

    def setUp(self):
        self.api = APIClient()
        self.api.force_authenticate(user=self.user)

    def _names(self, search):
        response = self.api.get("/es/api/sales/clients/", {"search": search})
        self.assertEqual(response.status_code, 200)
        return [row["name"] for row in response.json()["results"]]

    def test_search_matches_tax_identifier_name_and_commercial_name(self):
        self.assertEqual(self._names("b1234"), ["Taller Norte"])
        self.assertEqual(self._names("Taller Sur"), ["Taller Sur"])
        self.assertEqual(self._names("norte"), ["Taller Norte"])
        self.assertEqual(self._names("no-existe"), [])
