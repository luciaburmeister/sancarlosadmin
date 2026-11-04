"""Public API access: keys, the user's permissions, company scope, rate limit — and the mobile API untouched."""
from datetime import timedelta

from django.test import TestCase, override_settings
from django.urls import resolve
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework_api_key.models import APIKey
from rest_framework_simplejwt.tokens import RefreshToken

from api.billing.views.expediente_invoices import SalesInvoiceListView as MobileSalesInvoiceListView
from apps.public_api.models import UserAPIKey
from apps.public_api.tests.factories import auth_header, make_api_key, make_api_user
from company.models import CompanyUser
from invoices.tests.factories import make_company

LIST_URL = "/api/v1/sales-invoices/"


class _Base(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = make_company()
        cls.user = make_api_user(company=cls.company, perms=["invoices.view_salesinvoice"])

    def setUp(self):
        self.api = APIClient()

    def get(self, secret=None, url=LIST_URL, **headers):
        if secret is not None:
            headers.update(auth_header(secret))
        return self.api.get(url, **headers)


class KeyAuthenticationTests(_Base):
    def test_valid_key_lists(self):
        _, secret = make_api_key(user=self.user, company=self.company)
        response = self.get(secret)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["results"], [])

    def test_missing_key_is_401_with_error_envelope(self):
        response = self.get()
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response["WWW-Authenticate"], "Api-Key")
        self.assertEqual(response.json()["error"]["code"], "not_authenticated")

    def test_wrong_secret_is_401(self):
        key, secret = make_api_key(user=self.user, company=self.company)
        response = self.get(f"{key.prefix}.not-the-secret")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"]["code"], "invalid_api_key")

    def test_unknown_key_is_401(self):
        self.assertEqual(self.get("nope.nope").status_code, 401)

    def test_revoked_key_is_401(self):
        key, secret = make_api_key(user=self.user, company=self.company)
        key.revoked = True
        key.save()
        self.assertEqual(self.get(secret).status_code, 401)

    def test_expired_key_is_401(self):
        _, secret = make_api_key(
            user=self.user, company=self.company, expiry_date=timezone.now() - timedelta(minutes=1),
        )
        response = self.get(secret)
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"]["code"], "api_key_expired")

    def test_inactive_user_is_401(self):
        user = make_api_user(company=self.company, username="gone", perms=["invoices.view_salesinvoice"])
        _, secret = make_api_key(user=user, company=self.company)
        user.is_active = False
        user.save()
        self.assertEqual(self.get(secret).status_code, 401)

    def test_user_without_access_to_the_company_is_401(self):
        user = make_api_user(company=self.company, username="left", perms=["invoices.view_salesinvoice"])
        _, secret = make_api_key(user=user, company=self.company)
        CompanyUser.objects.filter(user=user).update(is_active=False)
        self.assertEqual(self.get(secret).status_code, 401)

    def test_jwt_is_not_accepted(self):
        token = str(RefreshToken.for_user(self.user).access_token)
        self.assertEqual(self.get(HTTP_AUTHORIZATION=f"Bearer {token}").status_code, 401)

    def test_legacy_ownerless_key_is_not_accepted(self):
        _, secret = APIKey.objects.create_key(name="cortizo")
        self.assertEqual(self.get(secret).status_code, 401)

    def test_records_last_use(self):
        key, secret = make_api_key(user=self.user, company=self.company)
        self.get(secret)
        key.refresh_from_db()
        self.assertIsNotNone(key.last_used_at)

    def test_errors_are_english_whatever_the_client_asks(self):
        response = self.get(HTTP_ACCEPT_LANGUAGE="es")
        self.assertEqual(response.json()["error"]["message"], "Authentication credentials were not provided.")


class PermissionTests(_Base):
    def test_user_without_view_permission_is_403(self):
        user = make_api_user(company=self.company, username="noperm")
        _, secret = make_api_key(user=user, company=self.company)
        response = self.get(secret)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "permission_denied")

    def test_create_needs_add_permission(self):
        _, secret = make_api_key(user=self.user, company=self.company)
        response = self.api.post(LIST_URL, {}, format="json", **auth_header(secret))
        self.assertEqual(response.status_code, 403)

    def test_unhandled_method_is_405(self):
        _, secret = make_api_key(user=self.user, company=self.company)
        response = self.api.delete(LIST_URL, **auth_header(secret))
        self.assertEqual(response.status_code, 405)


class RateLimitTests(_Base):
    @override_settings(PUBLIC_API_RATE="2/min")
    def test_over_the_rate_is_429(self):
        _, secret = make_api_key(user=self.user, company=self.company)
        self.assertEqual(self.get(secret).status_code, 200)
        self.assertEqual(self.get(secret).status_code, 200)
        response = self.get(secret)
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.json()["error"]["code"], "throttled")
        self.assertIn("Retry-After", response)


class UserAPIKeyModelTests(_Base):
    def test_clean_refuses_a_company_the_user_cannot_access(self):
        from django.core.exceptions import ValidationError

        other = make_company(name="Other Co")
        key = UserAPIKey(name="x", user=self.user, company=other)
        with self.assertRaises(ValidationError):
            key.clean()


class ExistingApiUntouchedTests(_Base):
    def test_mobile_routes_resolve_to_the_same_views(self):
        self.assertIs(resolve("/es/api/sales-invoices/").func.view_class, MobileSalesInvoiceListView)

    def test_cortizo_endpoint_still_takes_the_ownerless_key(self):
        _, secret = APIKey.objects.create_key(name="cortizo")
        response = self.api.post("/es/api/cortizo/import/", [], format="json", **auth_header(secret))
        # Past the permission check: the empty list is refused by the view itself
        self.assertEqual(response.status_code, 400)

    def test_public_key_does_not_open_the_cortizo_endpoint(self):
        _, secret = make_api_key(user=self.user, company=self.company)
        response = self.api.post("/es/api/cortizo/import/", [], format="json", **auth_header(secret))
        self.assertIn(response.status_code, (401, 403))
