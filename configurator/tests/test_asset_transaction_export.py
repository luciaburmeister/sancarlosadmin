from django.test import TestCase
from django.utils import timezone

from assets.models import Asset, AssetTransaction, AssetType, TransactionTypeChoices
from company.models import Company, WorkingZone
from configurator.services.export.asset_transaction import AssetTransactionExportStrategy
from management.models import ProjectFile
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


class AssetTransactionExportWorkingZoneContextTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name="Taller Co")
        cls.asset_type = AssetType.objects.create(company=cls.company, name="Cuba")
        cls.sales_client = Client.objects.create(company=cls.company, name="Obra Sur S.L.")
        cls.asset = Asset.objects.create(
            company=cls.company,
            asset_type=cls.asset_type,
            name="Cuba 1",
            client=cls.sales_client,
        )
        cls.project_file = ProjectFile.objects.create(
            company=cls.company,
            client=cls.sales_client,
            name="Reforma Málaga",
        )
        WorkingZone.objects.create(
            company=cls.company,
            project_file=cls.project_file,
            name="Obra centro",
            street_address="Calle Larios 1",
            city="Málaga",
            postal_code="29001",
            province="Málaga",
            cnae="3811",
            nima="2900012345",
        )
        WorkingZone.objects.create(
            company=cls.company,
            project_file=cls.project_file,
            name="Obra este",
            city="Málaga",
            province="Málaga",
        )
        WorkingZone.objects.create(
            company=cls.company,
            project_file=cls.project_file,
            name="Vertedero",
            zone_type=WorkingZone.ZoneType.VERTEDERO,
            city=" Antequera ",
            province="",
        )
        cls.transaction = AssetTransaction.objects.create(
            company=cls.company,
            asset=cls.asset,
            project_file=cls.project_file,
            transaction_type=TransactionTypeChoices.MOVEMENT,
            date=timezone.now(),
        )

    def test_project_file_working_zone_city_and_province_are_in_the_context(self):
        context = AssetTransactionExportStrategy(self.transaction).get_context()

        self.assertEqual(
            context["project_file_working_zones"],
            [
                {
                    "name": "Obra centro",
                    "zone_type": "obra",
                    "zone_type_display": "Obra",
                    "street_address": "Calle Larios 1",
                    "city": "Málaga",
                    "postal_code": "29001",
                    "province": "Málaga",
                    "country": "ES",
                    "full_address": "Calle Larios 1, Málaga, 29001",
                    "cnae": "3811",
                    "nima": "2900012345",
                },
                {
                    "name": "Obra este",
                    "zone_type": "obra",
                    "zone_type_display": "Obra",
                    "street_address": "",
                    "city": "Málaga",
                    "postal_code": "",
                    "province": "Málaga",
                    "country": "ES",
                    "full_address": "Málaga",
                    "cnae": "",
                    "nima": "",
                },
                {
                    "name": "Vertedero",
                    "zone_type": "vertedero",
                    "zone_type_display": "Vertedero",
                    "street_address": "",
                    "city": " Antequera ",
                    "postal_code": "",
                    "province": "",
                    "country": "ES",
                    "full_address": " Antequera ",
                    "cnae": "",
                    "nima": "",
                },
            ],
        )
        self.assertEqual(context["project_file_zone_type"], "Obra, Vertedero")
        self.assertEqual(
            context["project_file_address"],
            "Calle Larios 1, Málaga, 29001, Málaga, Antequera",
        )
        self.assertEqual(context["project_file_postal_code"], "29001")
        self.assertEqual(context["project_file_city"], "Málaga, Antequera")
        self.assertEqual(context["project_file_province"], "Málaga")
        self.assertEqual(context["project_file_cnae"], "3811")
        self.assertEqual(context["project_file_nima"], "2900012345")

    def test_missing_project_file_yields_empty_zone_fields(self):
        transaction = AssetTransaction.objects.create(
            company=self.company,
            asset=self.asset,
            transaction_type=TransactionTypeChoices.MOVEMENT,
            date=timezone.now(),
        )

        context = AssetTransactionExportStrategy(transaction).get_context()

        self.assertEqual(context["project_file_working_zones"], [])
        self.assertEqual(context["project_file_zone_type"], "")
        self.assertEqual(context["project_file_address"], "")
        self.assertEqual(context["project_file_postal_code"], "")
        self.assertEqual(context["project_file_city"], "")
        self.assertEqual(context["project_file_province"], "")
        self.assertEqual(context["project_file_cnae"], "")
        self.assertEqual(context["project_file_nima"], "")
