import tablib
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import translation

from company.models import Company
from materials.models import PaymentMethod, Provider
from materials.resources import ProviderResource


def _column(field_name):
    """The exported header for a field, in the language the test runs in."""
    return str(ProviderResource().fields[field_name].column_name)


class ProviderExportPaymentMethodTests(TestCase):
    """The export carries the payment method as a readable label."""

    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name="ACME")
        cls.provider = Provider.objects.create(
            name="Cortizo",
            company=cls.company,
            payment_method=PaymentMethod.CONFIRMING,
        )

    def test_export_includes_payment_method_label(self):
        dataset = ProviderResource().export(Provider.objects.filter(pk=self.provider.pk))

        column = _column("payment_method")
        self.assertIn(column, [str(header) for header in dataset.headers])
        self.assertEqual(dataset.dict[0][column], str(PaymentMethod.CONFIRMING.label))


class ProviderImportPaymentMethodTests(TestCase):
    """The exported file can be edited and imported back to set payment methods."""

    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name="ACME")
        cls.other_company = Company.objects.create(name="Other")
        cls.provider = Provider.objects.create(
            name="Cortizo",
            company=cls.company,
            payment_method=PaymentMethod.TRANSFER,
        )
        cls.foreign_provider = Provider.objects.create(
            name="Outsider",
            company=cls.other_company,
            payment_method=PaymentMethod.TRANSFER,
        )

    def _import(self, rows, headers=None, **kwargs):
        headers = headers or ("id", _column("payment_method"))
        dataset = tablib.Dataset(*rows, headers=list(headers))
        resource = ProviderResource(**kwargs)
        return resource.import_data(dataset, dry_run=False, raise_errors=False)

    def _refresh(self):
        self.provider.refresh_from_db()
        return self.provider.payment_method

    def test_import_updates_payment_method_from_label(self):
        result = self._import([(self.provider.pk, str(PaymentMethod.CHEQUE.label))])

        self.assertFalse(result.has_errors())
        self.assertFalse(result.has_validation_errors())
        self.assertEqual(self._refresh(), PaymentMethod.CHEQUE)

    def test_import_accepts_the_stored_code(self):
        result = self._import([(self.provider.pk, "sepa_direct_debit")])

        self.assertFalse(result.has_errors())
        self.assertEqual(self._refresh(), PaymentMethod.SEPA_DIRECT_DEBIT)

    def test_import_accepts_a_label_from_another_language(self):
        """A file exported in one language still imports from a session in another."""
        other = "en" if translation.get_language() != "en" else "es"
        with translation.override(other):
            label = str(PaymentMethod.CARD.label)

        result = self._import([(self.provider.pk, label)])

        self.assertFalse(result.has_errors())
        self.assertFalse(result.has_validation_errors())
        self.assertEqual(self._refresh(), PaymentMethod.CARD)

    def test_alternative_header_spellings_are_accepted(self):
        result = self._import(
            [(self.provider.pk, "cash")], headers=("ID", "Método de pago")
        )

        self.assertFalse(result.has_errors())
        self.assertEqual(self._refresh(), PaymentMethod.CASH)

    def test_ids_read_from_xlsx_as_floats_still_match(self):
        result = self._import([(float(self.provider.pk), "cash")])

        self.assertFalse(result.has_errors())
        self.assertFalse(result.has_validation_errors())
        self.assertEqual(self._refresh(), PaymentMethod.CASH)

    def test_blank_payment_method_keeps_the_current_one(self):
        result = self._import([(self.provider.pk, "")])

        self.assertFalse(result.has_errors())
        self.assertEqual(self._refresh(), PaymentMethod.TRANSFER)

    def test_unknown_payment_method_is_reported(self):
        result = self._import([(self.provider.pk, "bitcoin")])

        self.assertTrue(result.has_errors() or result.has_validation_errors())
        self.assertEqual(self._refresh(), PaymentMethod.TRANSFER)

    def test_unknown_id_does_not_create_a_provider(self):
        result = self._import([(999999, "cash")])

        self.assertTrue(result.has_validation_errors())
        self.assertEqual(Provider.objects.count(), 2)

    def test_import_cannot_reach_another_companys_provider(self):
        result = self._import(
            [(self.foreign_provider.pk, "cash")],
            allowed_companies=Company.objects.filter(pk=self.company.pk),
        )

        self.assertTrue(result.has_validation_errors())
        self.foreign_provider.refresh_from_db()
        self.assertEqual(self.foreign_provider.payment_method, PaymentMethod.TRANSFER)

    def test_only_the_payment_method_is_written_back(self):
        """Re-importing an edited export never overwrites the other columns."""
        dataset = ProviderResource().export(
            Provider.objects.filter(pk=self.provider.pk)
        )
        row = dataset.dict[0]
        row[_column("payment_method")] = str(PaymentMethod.CASH.label)
        row[_column("name")] = "Renamed"
        edited = tablib.Dataset(
            tuple(row.values()), headers=[str(h) for h in dataset.headers]
        )

        result = ProviderResource().import_data(edited, dry_run=False, raise_errors=False)

        self.assertFalse(result.has_errors())
        self.provider.refresh_from_db()
        self.assertEqual(self.provider.payment_method, PaymentMethod.CASH)
        self.assertEqual(self.provider.name, "Cortizo")


class ProviderAdminImportTests(TestCase):
    """The import is reachable from the Provider admin and gated by a permission."""

    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name="ACME")
        cls.provider = Provider.objects.create(
            name="Cortizo",
            company=cls.company,
            tax_identifier="B12345678",
            payment_method=PaymentMethod.TRANSFER,
        )
        cls.superuser = get_user_model().objects.create_superuser(
            username="importer", email="importer@example.com", password="secret"
        )
        cls.staff = get_user_model().objects.create_user(
            username="staff", email="staff@example.com", password="secret", is_staff=True
        )

    def test_staff_without_the_permission_cannot_import(self):
        self.client.force_login(self.staff)

        response = self.client.get(reverse("admin:materials_provider_import"))

        self.assertEqual(response.status_code, 403)

    # The default MediaStorage builds a new storage per instance, which loses
    # the uploaded file between the two import steps under the in-memory
    # storage tests run with.
    @override_settings(
        IMPORT_EXPORT_TMP_STORAGE_CLASS="import_export.tmp_storages.TempFolderStorage"
    )
    def test_admin_import_updates_the_payment_method(self):
        self.client.force_login(self.superuser)
        csv = f"id,{_column('payment_method')}\n{self.provider.pk},{PaymentMethod.CARD.label}\n"

        response = self.client.post(
            reverse("admin:materials_provider_import"),
            {
                "format": "0",
                "import_file": SimpleUploadedFile("providers.csv", csv.encode()),
                "resource": "0",
            },
        )
        self.assertEqual(response.status_code, 200)
        confirm_form = response.context["confirm_form"]

        response = self.client.post(
            reverse("admin:materials_provider_process_import"),
            confirm_form.initial,
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.provider.refresh_from_db()
        self.assertEqual(self.provider.payment_method, PaymentMethod.CARD)
