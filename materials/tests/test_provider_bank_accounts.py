from types import SimpleNamespace

from django.contrib.admin.sites import AdminSite
from django.contrib.auth import get_user_model
from django.test import RequestFactory, TestCase

from company.models import Company
from materials.admin import ProviderAdmin, ProviderBankAccountInline
from materials.models import Provider, ProviderBankAccount


class ProviderBankAccountInlineTest(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Company A")
        self.provider = Provider.objects.create(
            name="Provider One", company=self.company, tax_identifier="B12345678"
        )
        self.request = RequestFactory().post("/")
        self.request.user = get_user_model().objects.create_superuser(
            username="admin", email="admin@example.com", password="x"
        )
        site = AdminSite()
        self.model_admin = ProviderAdmin(Provider, site)
        self.inline = ProviderBankAccountInline(Provider, site)

    def _formset(self, **fields):
        FormSet = self.inline.get_formset(self.request, self.provider)
        prefix = FormSet.get_default_prefix()
        data = {
            f"{prefix}-TOTAL_FORMS": "1",
            f"{prefix}-INITIAL_FORMS": "0",
            f"{prefix}-MIN_NUM_FORMS": "0",
            f"{prefix}-MAX_NUM_FORMS": "1000",
        }
        data.update({f"{prefix}-0-{name}": value for name, value in fields.items()})
        return FormSet(data=data, instance=self.provider)

    def test_bank_account_inherits_the_provider_company(self):
        # IBAN matching of bank transactions filters by company, so an account
        # saved without one would never match.
        formset = self._formset(bank_name="Santander", iban="ES9121000418450200051332")
        self.assertTrue(formset.is_valid(), formset.errors)

        self.model_admin.save_formset(
            self.request,
            form=SimpleNamespace(instance=self.provider),
            formset=formset,
            change=True,
        )

        account = ProviderBankAccount.objects.get(provider=self.provider)
        self.assertEqual(account.company, self.company)
        self.assertEqual(account.iban, "ES9121000418450200051332")

    def test_existing_company_on_the_account_is_kept(self):
        other_company = Company.objects.create(name="Company B")
        account = ProviderBankAccount.objects.create(
            provider=self.provider, company=other_company, bank_name="Santander"
        )

        account.bank_name = "BBVA"
        formset = SimpleNamespace(
            save=lambda commit: [account],
            deleted_objects=[],
            save_m2m=lambda: None,
        )
        self.model_admin.save_formset(
            self.request,
            form=SimpleNamespace(instance=self.provider),
            formset=formset,
            change=True,
        )

        account.refresh_from_db()
        self.assertEqual(account.company, other_company)
        self.assertEqual(account.bank_name, "BBVA")
