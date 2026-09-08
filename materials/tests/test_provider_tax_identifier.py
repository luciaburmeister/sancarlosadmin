from django.core.exceptions import ValidationError
from django.test import TestCase

from company.models import Company
from materials.admin import ProviderForm
from materials.models import PaymentMethod, Provider


class ProviderTaxIdentifierUniquenessTest(TestCase):
    def setUp(self):
        self.company_a = Company.objects.create(name="Company A")
        self.company_b = Company.objects.create(name="Company B")

    def test_rejects_duplicate_tax_identifier_within_same_company(self):
        Provider.objects.create(
            name="Provider One", company=self.company_a, tax_identifier="B12345678"
        )
        duplicate = Provider(
            name="Provider Two", company=self.company_a, tax_identifier="B12345678"
        )

        with self.assertRaises(ValidationError):
            duplicate.save()

    def test_rejects_duplicate_tax_identifier_case_insensitively(self):
        Provider.objects.create(
            name="Provider One", company=self.company_a, tax_identifier="b12345678"
        )
        duplicate = Provider(
            name="Provider Two", company=self.company_a, tax_identifier="B12345678"
        )

        with self.assertRaises(ValidationError):
            duplicate.save()

    def test_allows_same_tax_identifier_across_different_companies(self):
        Provider.objects.create(
            name="Provider One", company=self.company_a, tax_identifier="B12345678"
        )
        other_company_provider = Provider(
            name="Provider Two", company=self.company_b, tax_identifier="B12345678"
        )

        other_company_provider.save()  # should not raise

        self.assertTrue(
            Provider.objects.filter(
                company=self.company_b, tax_identifier="B12345678"
            ).exists()
        )

    def test_updating_the_same_provider_does_not_conflict_with_itself(self):
        provider = Provider.objects.create(
            name="Provider One", company=self.company_a, tax_identifier="B12345678"
        )

        provider.contact = "Updated contact"
        provider.save()  # should not raise

    def test_blank_tax_identifier_does_not_trigger_uniqueness_check(self):
        # Automated import flows may create providers before a tax id is
        # known; multiple blank identifiers within the same company must
        # remain possible.
        Provider.objects.create(name="Provider One", company=self.company_a)
        second = Provider(name="Provider Two", company=self.company_a)

        second.save()  # should not raise

    def test_clean_also_reports_the_duplicate(self):
        Provider.objects.create(
            name="Provider One", company=self.company_a, tax_identifier="B12345678"
        )
        duplicate = Provider(
            name="Provider Two", company=self.company_a, tax_identifier="B12345678"
        )

        with self.assertRaises(ValidationError) as ctx:
            duplicate.clean()

        self.assertIn("tax_identifier", ctx.exception.message_dict)


class ProviderAdminFormRequiresTaxIdentifierTest(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Company A")

    def _form_data(self, **overrides):
        data = {
            "name": "Provider One",
            "company": self.company.id,
            "tax_identifier": "B12345678",
            "associated_emails": "",
            "payment_method": PaymentMethod.TRANSFER,
            "payment_terms_days": 0,
        }
        data.update(overrides)
        return data

    def test_form_rejects_blank_tax_identifier(self):
        form = ProviderForm(data=self._form_data(tax_identifier=""))

        self.assertFalse(form.is_valid())
        self.assertIn("tax_identifier", form.errors)

    def test_form_accepts_a_tax_identifier(self):
        form = ProviderForm(data=self._form_data())

        self.assertTrue(form.is_valid(), form.errors)
