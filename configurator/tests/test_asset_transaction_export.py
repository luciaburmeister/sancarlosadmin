from django.test import TestCase
from django.utils import timezone

from assets.models import Asset, AssetTransaction, AssetType, TransactionTypeChoices
from company.models import Company
from configurator.services.export.asset_transaction import AssetTransactionExportStrategy
from sales.models import Client


class AssetTransactionExportClientContextTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name="Taller Co")
        cls.asset_type = AssetType.objects.create(company=cls.company, name="Cuba")
        cls.sales_client = Client.objects.create(
            company=cls.company,
            name="Obra Sur S.L.",
            phone="952000000",
            tax_identifier="B12345678",
            nima="2900012345",
        )
        cls.asset = Asset.objects.create(
            company=cls.company,
            asset_type=cls.asset_type,
            name="Cuba 1",
            client=cls.sales_client,
        )
        cls.transaction = AssetTransaction.objects.create(
            company=cls.company,
            asset=cls.asset,
            transaction_type=TransactionTypeChoices.MOVEMENT,
            date=timezone.now(),
        )

    def test_client_contact_fields_are_in_the_template_context(self):
        context = AssetTransactionExportStrategy(self.transaction).get_context()

        self.assertEqual(context["client"], self.sales_client)
        self.assertEqual(context["client_name"], "Obra Sur S.L.")
        self.assertEqual(context["client_phone"], "952000000")
        self.assertEqual(context["client_tax_id"], "B12345678")
        self.assertEqual(context["client_nima"], "2900012345")

    def test_missing_client_yields_empty_contact_fields(self):
        asset = Asset.objects.create(
            company=self.company,
            asset_type=self.asset_type,
            name="Cuba sin cliente",
        )
        transaction = AssetTransaction.objects.create(
            company=self.company,
            asset=asset,
            transaction_type=TransactionTypeChoices.MOVEMENT,
            date=timezone.now(),
        )

        context = AssetTransactionExportStrategy(transaction).get_context()

        self.assertIsNone(context["client"])
        self.assertIsNone(context["client_name"])
        self.assertEqual(context["client_phone"], "")
        self.assertEqual(context["client_tax_id"], "")
        self.assertEqual(context["client_nima"], "")
