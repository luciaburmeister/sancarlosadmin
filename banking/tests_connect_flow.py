"""
The bank connection started from a company's setup: locked to that company,
and back to the setup when the bank is done.
"""
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.test import TestCase
from django.urls import reverse

from banking.services.arcopay_import import ArcopayService
from company.models import Company, CompanyUser

User = get_user_model()

NEXT = "/es/admin/company/company/1/onboarding/bank/"


class ConnectFlowTest(TestCase):
    def setUp(self):
        # The user's first company is the one switched on by default; the
        # flow must work for the other one too
        self.switched_on = Company.objects.create(name="A Co")
        self.company = Company.objects.create(name="B Co")
        self.stranger = Company.objects.create(name="C Co")
        self.user = User.objects.create_user("ana", "ana@b.es", "x", is_staff=True)
        self.user.user_permissions.add(Permission.objects.get(codename="view_banktransaction"))
        for company in (self.switched_on, self.company):
            CompanyUser.objects.create(user=self.user, company=company)
        self.client.force_login(self.user)

    def initiate(self, company, next_url=NEXT):
        with patch.object(ArcopayService, "get_consent", return_value={
            "follow": "https://bank.example.com/auth", "consentId": "c-1", "token": "t-1",
        }):
            return self.client.post(reverse("admin:banking_initiate_consent"), {
                "company_id": company.pk, "bank_service": "santander", "next": next_url,
            })

    def test_connect_page_is_locked_to_the_requested_company(self):
        with patch.object(ArcopayService, "list_supported_banks", return_value=[]):
            response = self.client.get(
                reverse("admin:banking_connect_bank"), {"company_id": self.company.pk, "next": NEXT},
            )

        self.assertEqual(list(response.context["companies"]), [self.company])
        self.assertEqual(response.context["next_url"], NEXT)

    def test_consent_for_one_of_the_users_companies_goes_to_the_bank(self):
        response = self.initiate(self.company)

        self.assertRedirects(response, "https://bank.example.com/auth", fetch_redirect_response=False)
        self.assertEqual(self.client.session["arcopay_next"], NEXT)

    def test_consent_for_someone_elses_company_is_refused(self):
        response = self.initiate(self.stranger)

        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.url.startswith(reverse("admin:banking_connect_bank")))
        self.assertIn(f"company_id={self.stranger.pk}", response.url)
        self.assertNotIn("arcopay_next", self.client.session)

    def test_return_page_on_another_site_is_ignored(self):
        self.initiate(self.company, next_url="https://evil.example.com/")

        self.assertNotIn("arcopay_next", self.client.session)

    def test_back_from_the_bank_returns_to_the_setup(self):
        self.initiate(self.company)

        with patch.object(ArcopayService, "store_accounts_from_token", return_value=[]):
            response = self.client.get(reverse("admin:banking_consent_done"), {"statusAB": "200"})

        self.assertRedirects(response, NEXT, fetch_redirect_response=False)

    def test_failed_bank_authorization_also_returns_to_the_setup(self):
        self.initiate(self.company)

        response = self.client.get(reverse("admin:banking_consent_done"), {"statusAB": "500"})

        self.assertRedirects(response, NEXT, fetch_redirect_response=False)

    def test_without_a_return_page_it_lands_where_it_always_did(self):
        self.initiate(self.company, next_url="")

        response = self.client.get(reverse("admin:banking_consent_done"), {"statusAB": "500"})

        self.assertRedirects(
            response, reverse("admin:banking_banktransaction_changelist"), fetch_redirect_response=False,
        )
