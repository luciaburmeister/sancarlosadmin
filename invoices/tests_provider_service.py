from django.test import TestCase

from company.models import Company
from materials.models import Provider
from core.services.provider_service import get_or_create_provider


class GetOrCreateProviderTest(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Global Corp")

    def test_no_tax_id_returns_none_without_checking_email(self):
        Provider.objects.create(
            name="Acme Ltd",
            tax_identifier="B12345678",
            company=self.company,
            associated_emails=["billing@acme.com"],
        )

        provider = get_or_create_provider(None, "Acme Ltd", self.company, sender_email="billing@acme.com")

        self.assertIsNone(provider)

    def test_matches_existing_provider_by_tax_id(self):
        existing = Provider.objects.create(
            name="Acme Ltd",
            tax_identifier="B12345678",
            company=self.company,
        )

        provider = get_or_create_provider("B12345678", "Acme Ltd", self.company)

        self.assertEqual(provider, existing)

    def test_creates_provider_when_tax_id_unmatched(self):
        provider = get_or_create_provider("B99999999", "New Vendor", self.company)

        self.assertIsNotNone(provider)
        self.assertEqual(provider.tax_identifier, "B99999999")
        self.assertEqual(provider.name, "New Vendor")

    def test_matching_tax_id_wins_even_if_email_belongs_to_another_provider(self):
        other = Provider.objects.create(
            name="Other Vendor",
            tax_identifier="B00000000",
            company=self.company,
            associated_emails=["billing@shared-mailbox.com"],
        )
        target = Provider.objects.create(
            name="Acme Ltd",
            tax_identifier="B12345678",
            company=self.company,
        )

        provider = get_or_create_provider(
            "B12345678", "Acme Ltd", self.company, sender_email="billing@shared-mailbox.com"
        )

        # Tax ID decides identity — "other" having this email on file never
        # causes the lookup to return "other" instead of "target".
        self.assertEqual(provider, target)

        # The email still gets recorded as a hint on whichever provider tax ID
        # resolved to, even though it was already on file for "other" too.
        # Enrichment isn't a uniqueness claim — it's fine for the same address
        # to appear against more than one provider.
        target.refresh_from_db()
        self.assertIn("billing@shared-mailbox.com", target.associated_emails)

    def test_sender_email_is_recorded_on_the_resolved_provider(self):
        provider = get_or_create_provider(
            "B12345678", "Acme Ltd", self.company, sender_email="Billing@Acme.com"
        )

        provider.refresh_from_db()
        self.assertIn("billing@acme.com", provider.associated_emails)

    def test_sender_email_is_not_duplicated_on_repeat_matches(self):
        Provider.objects.create(
            name="Acme Ltd",
            tax_identifier="B12345678",
            company=self.company,
            associated_emails=["billing@acme.com"],
        )

        provider = get_or_create_provider(
            "B12345678", "Acme Ltd", self.company, sender_email="billing@acme.com"
        )

        self.assertEqual(provider.associated_emails.count("billing@acme.com"), 1)
