"""
payments/testing.py

Fixtures shared by the payments and expenses test modules: the "Global Corp"
company every test runs under, admin GET/POST helpers that pin English, and
the register-payment form data.
"""
from django.contrib.auth import get_user_model
from django.urls import reverse
from django.utils import translation

from company.models import Company
from payments.models import PaymentMethod

User = get_user_model()


def payment_form(*, amount, reference="REF-1", payment_date="2026-07-03"):
    """The POST body of the Register Payment screen for ``amount`` EUR by transfer."""
    return {
        "amount": amount,
        "currency": "EUR",
        "payment_date": payment_date,
        "method": PaymentMethod.TRANSFER,
        "reference": reference,
    }


class CompanyAdminTestMixin:
    """Creates ``self.company`` and wraps admin requests in an English locale.

    Admin URLs are language-prefixed by i18n_patterns, so the prefix (not an
    Accept-Language header) decides the rendered language. LocaleMiddleware
    activates the URL's language and never restores it, so the request itself
    has to run inside the override or English would leak into later tests.
    """

    def setUp(self):
        super().setUp()
        self.company = Company.objects.create(name="Global Corp")

    def login_superuser(self, *, username="finance", email="fin@example.com"):
        """Create a superuser, log the test client in as them and return them."""
        user = User.objects.create_superuser(username=username, password="pw", email=email)
        self.client.force_login(user)
        return user

    def _get(self, url_name, *args):
        with translation.override("en"):
            return self.client.get(reverse(url_name, args=args))

    def _post(self, url_name, *args, data):
        with translation.override("en"):
            return self.client.post(reverse(url_name, args=args), data)
