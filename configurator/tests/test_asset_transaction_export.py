from django.test import TestCase
from django.utils import timezone

from assets.models import Asset, AssetTransaction, AssetTransactionReceipt, AssetType, TransactionTypeChoices
from company.models import Company, WorkingZone
from configurator.services.export.asset_transaction import AssetTransactionExportStrategy
from management.models import ProjectFile
from sales.models import Client


class _AssetTransactionExportTestCase(TestCase):
    """One company with one "Cuba" asset; subclasses add the rest."""

    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name="Taller Co")
        cls.asset_type = AssetType.objects.create(company=cls.company, name="Cuba")
        cls.sales_client = cls.make_asset_client()
        cls.asset = Asset.objects.create(
            company=cls.company,
            asset_type=cls.asset_type,
            name="Cuba 1",
            client=cls.sales_client,
        )

    @classmethod
    def make_asset_client(cls):
        """Return the client the asset is rented to (none by default)."""
        return None


class AssetTransactionExportClientContextTests(_AssetTransactionExportTestCase):
    @classmethod
    def make_asset_client(cls):
        return Client.objects.create(
            company=cls.company,
            name="Obra Sur S.L.",
            phone="952000000",
            tax_identifier="B12345678",
            nima="2900012345",
        )

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
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

    def test_project_file_client_takes_priority_over_asset_client(self):
        project_client = Client.objects.create(
            company=self.company,
            name="Expediente Norte S.L.",
            phone="600111222",
            tax_identifier="B99999999",
            nima="2900099999",
        )
        project_file = ProjectFile.objects.create(
            company=self.company,
            client=project_client,
            name="Expediente Norte",
        )
        transaction = AssetTransaction.objects.create(
            company=self.company,
            asset=self.asset,
            project_file=project_file,
            transaction_type=TransactionTypeChoices.MOVEMENT,
            date=timezone.now(),
        )

        context = AssetTransactionExportStrategy(transaction).get_context()

        self.assertEqual(context["client"], project_client)
        self.assertEqual(context["client_name"], "Expediente Norte S.L.")
        self.assertEqual(context["client_phone"], "600111222")
        self.assertEqual(context["client_tax_id"], "B99999999")
        self.assertEqual(context["client_nima"], "2900099999")


class AssetTransactionExportReferenceNumberContextTests(_AssetTransactionExportTestCase):
    """reference_number moved from a `data` JSON key to a real model field
    (migration 0078), but every template -- including ones already live in
    production -- reads it as `{{ data.reference_number }}`. These prove
    that read path is byte-identical regardless of where the value actually
    lives now."""

    def test_model_field_value_appears_under_data_reference_number(self):
        transaction = AssetTransaction.objects.create(
            company=self.company, asset=self.asset,
            transaction_type=TransactionTypeChoices.MOVEMENT, date=timezone.now(),
            reference_number="REF-99999", data={"tipo_cuba": "RCD"},
        )

        context = AssetTransactionExportStrategy(transaction).get_context()

        self.assertEqual(context["data"]["reference_number"], "REF-99999")
        # Nothing else in `data` gets disturbed by the splice.
        self.assertEqual(context["data"]["tipo_cuba"], "RCD")

    def test_no_reference_number_anywhere_yields_empty_string(self):
        transaction = AssetTransaction.objects.create(
            company=self.company, asset=self.asset,
            transaction_type=TransactionTypeChoices.MOVEMENT, date=timezone.now(),
        )

        context = AssetTransactionExportStrategy(transaction).get_context()

        self.assertEqual(context["data"]["reference_number"], "")

    def test_falls_back_to_the_legacy_data_key_if_never_backfilled(self):
        """A pre-migration row that never got the backfill query run against
        it: the model field is still blank, but the old `data` key is there
        -- the template must still see the value, not silently lose it."""
        transaction = AssetTransaction.objects.create(
            company=self.company, asset=self.asset,
            transaction_type=TransactionTypeChoices.MOVEMENT, date=timezone.now(),
            data={"reference_number": "OLD-LEGACY-REF"},
        )

        context = AssetTransactionExportStrategy(transaction).get_context()

        self.assertEqual(context["data"]["reference_number"], "OLD-LEGACY-REF")

    def test_model_field_takes_priority_over_a_stale_legacy_data_key(self):
        """If a row somehow has both (e.g. backfilled without cleaning the
        old key), the real field is the authoritative source going forward."""
        transaction = AssetTransaction.objects.create(
            company=self.company, asset=self.asset,
            transaction_type=TransactionTypeChoices.MOVEMENT, date=timezone.now(),
            reference_number="NEW-REF", data={"reference_number": "OLD-LEGACY-REF"},
        )

        context = AssetTransactionExportStrategy(transaction).get_context()

        self.assertEqual(context["data"]["reference_number"], "NEW-REF")


class AssetTransactionExportWorkingZoneContextTests(_AssetTransactionExportTestCase):
    @classmethod
    def make_asset_client(cls):
        return Client.objects.create(company=cls.company, name="Obra Sur S.L.")

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
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
            cif="B12345678",
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
                    "cif": "B12345678",
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
                    "cif": "",
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
                    "cif": "",
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


class AssetTransactionExportLandfillContextTests(_AssetTransactionExportTestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.landfill = WorkingZone.objects.create(
            company=cls.company,
            name="Reciclados San Carlos",
            zone_type=WorkingZone.ZoneType.VERTEDERO,
            street_address="Polígono 1",
            city="Antequera",
            postal_code="29200",
            province="Málaga",
            cnae="3821",
            nima="2900099999",
            cif="B92198522",
        )
        cls.warehouse = WorkingZone.objects.create(
            company=cls.company,
            name="Almacén",
            zone_type=WorkingZone.ZoneType.ALMACEN,
        )

    def test_movement_resolves_the_named_landfill(self):
        transaction = AssetTransaction.objects.create(
            company=self.company,
            asset=self.asset,
            transaction_type=TransactionTypeChoices.MOVEMENT,
            to_zone=self.warehouse,
            date=timezone.now(),
            data={"vertedero": "Reciclados San Carlos"},
        )

        context = AssetTransactionExportStrategy(transaction).get_context()

        self.assertEqual(
            context["landfill"],
            {
                "name": "Reciclados San Carlos",
                "zone_type": "vertedero",
                "zone_type_display": "Vertedero",
                "street_address": "Polígono 1",
                "city": "Antequera",
                "postal_code": "29200",
                "province": "Málaga",
                "country": "ES",
                "full_address": "Polígono 1, Antequera, 29200",
                "cnae": "3821",
                "nima": "2900099999",
                "cif": "B92198522",
            },
        )

    def test_movement_without_a_matching_landfill_is_empty(self):
        transaction = AssetTransaction.objects.create(
            company=self.company,
            asset=self.asset,
            transaction_type=TransactionTypeChoices.MOVEMENT,
            date=timezone.now(),
            data={"vertedero": "No existe"},
        )

        context = AssetTransactionExportStrategy(transaction).get_context()

        self.assertIsNone(context["landfill"])

    def test_receipt_uses_its_destination_when_that_zone_is_a_landfill(self):
        transaction = AssetTransaction.objects.create(
            company=self.company,
            asset=self.asset,
            transaction_type=TransactionTypeChoices.RECEIPT,
            to_zone=self.landfill,
            date=timezone.now(),
        )

        context = AssetTransactionExportStrategy(transaction).get_context()

        self.assertEqual(context["landfill"]["name"], "Reciclados San Carlos")
        self.assertEqual(context["landfill"]["nima"], "2900099999")
        self.assertEqual(context["landfill"]["cif"], "B92198522")
        self.assertEqual(context["landfill"]["full_address"], "Polígono 1, Antequera, 29200")

    def test_receipt_destination_that_is_not_a_landfill_is_empty(self):
        transaction = AssetTransaction.objects.create(
            company=self.company,
            asset=self.asset,
            transaction_type=TransactionTypeChoices.RECEIPT,
            to_zone=self.warehouse,
            date=timezone.now(),
        )

        context = AssetTransactionExportStrategy(transaction).get_context()

        self.assertIsNone(context["landfill"])


class AssetTransactionExportReceiptMatriculaContextTests(_AssetTransactionExportTestCase):
    """A RECEIPT (verteo) transaction never records the truck plate itself --
    the mobile submission that creates it has no such field, only the earlier
    MOVEMENT that dropped off the cuba does. The export context falls back to
    that related movement's plate so receipt documents print it too."""

    def test_receipt_inherits_the_plate_from_its_related_movement(self):
        movement = AssetTransaction.objects.create(
            company=self.company, asset=self.asset,
            transaction_type=TransactionTypeChoices.MOVEMENT, date=timezone.now(),
            data={"matricula": "0709CBB"},
        )
        receipt_tx = AssetTransaction.objects.create(
            company=self.company, asset=self.asset,
            transaction_type=TransactionTypeChoices.RECEIPT, date=timezone.now(),
            data={"nro_albaran": "01083485"},
        )
        AssetTransactionReceipt.objects.create(
            transaction=receipt_tx, nro_albaran="01083485", movement_transaction=movement,
        )

        context = AssetTransactionExportStrategy(receipt_tx).get_context()

        self.assertEqual(context["data"]["matricula"], "0709CBB")
        # Nothing else in `data` gets disturbed by the splice.
        self.assertEqual(context["data"]["nro_albaran"], "01083485")

    def test_receipt_without_a_related_movement_has_no_plate(self):
        receipt_tx = AssetTransaction.objects.create(
            company=self.company, asset=self.asset,
            transaction_type=TransactionTypeChoices.RECEIPT, date=timezone.now(),
            data={"nro_albaran": "01083485"},
        )

        context = AssetTransactionExportStrategy(receipt_tx).get_context()

        self.assertNotIn("matricula", context["data"])

    def test_receipt_own_plate_is_not_overridden_by_the_movement(self):
        movement = AssetTransaction.objects.create(
            company=self.company, asset=self.asset,
            transaction_type=TransactionTypeChoices.MOVEMENT, date=timezone.now(),
            data={"matricula": "0709CBB"},
        )
        receipt_tx = AssetTransaction.objects.create(
            company=self.company, asset=self.asset,
            transaction_type=TransactionTypeChoices.RECEIPT, date=timezone.now(),
            data={"matricula": "1111AAA"},
        )
        AssetTransactionReceipt.objects.create(
            transaction=receipt_tx, nro_albaran="01083485", movement_transaction=movement,
        )

        context = AssetTransactionExportStrategy(receipt_tx).get_context()

        self.assertEqual(context["data"]["matricula"], "1111AAA")

    def test_movement_transaction_is_unaffected(self):
        movement = AssetTransaction.objects.create(
            company=self.company, asset=self.asset,
            transaction_type=TransactionTypeChoices.MOVEMENT, date=timezone.now(),
            data={"matricula": "0709CBB"},
        )

        context = AssetTransactionExportStrategy(movement).get_context()

        self.assertEqual(context["data"]["matricula"], "0709CBB")
