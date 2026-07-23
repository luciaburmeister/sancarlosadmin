"""Tests for the two-step asset creation wizard."""
from types import SimpleNamespace

from django import forms
from django.test import TestCase, RequestFactory

from assets.forms import AssetWizardBasicsForm, AssetWizardDetailsForm
from assets.models import Asset, AssetType, AssetTypeField
from assets.views.asset_wizard import AssetCreateWizardView
from company.models import Company
from sales.models import Client


class AssetWizardFormsTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Taller Co")
        self.other_company = Company.objects.create(name="Other Co")
        self.asset_type = AssetType.objects.create(
            name="Vehículo", company=self.company,
            identifier_labels=["Matrícula", "Bastidor"],
        )
        AssetTypeField.objects.create(
            asset_type=self.asset_type, name="marca", label="Marca",
            type="text", required=True, order=1,
        )
        AssetTypeField.objects.create(
            asset_type=self.asset_type, name="anio", label="Año", type="number", order=2,
        )

    def test_details_form_builds_identifier_and_data_fields(self):
        form = AssetWizardDetailsForm(asset_type=self.asset_type)
        self.assertIn("identifier__Matrícula", form.fields)
        self.assertIn("identifier__Bastidor", form.fields)
        self.assertIn("asset_data__marca", form.fields)
        self.assertIn("asset_data__anio", form.fields)
        self.assertTrue(form.fields["asset_data__marca"].required)
        self.assertIsInstance(form.fields["identifier__Matrícula"], forms.CharField)

    def test_details_form_empty_without_type(self):
        self.assertEqual(AssetWizardDetailsForm(asset_type=None).fields, {})

    def test_basics_form_rejects_cross_company_type(self):
        foreign_type = AssetType.objects.create(name="Cuba", company=self.other_company)
        form = AssetWizardBasicsForm(data={
            "name": "X", "asset_type": foreign_type.pk, "company": self.company.pk,
        })
        self.assertFalse(form.is_valid())
        self.assertIn("asset_type", form.errors)


class AssetWizardDoneTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.company = Company.objects.create(name="Taller Co")
        self.client_obj = Client.objects.create(name="Acme", company=self.company)
        self.asset_type = AssetType.objects.create(
            name="Vehículo", company=self.company,
            identifier_labels=["Matrícula", "Bastidor"],
        )
        AssetTypeField.objects.create(
            asset_type=self.asset_type, name="marca", label="Marca", type="text", order=1,
        )

    def _run_done(self, popup=False):
        view = AssetCreateWizardView(model_admin=None)
        view.request = self.factory.post("/")
        view.storage = SimpleNamespace(
            extra_data={"popup": "1", "to_field": "id"} if popup else {}
        )

        basics = AssetWizardBasicsForm(data={
            "name": "Wizard Test Arona",
            "asset_type": self.asset_type.pk,
            "client": self.client_obj.pk,
            "company": self.company.pk,
        })
        self.assertTrue(basics.is_valid(), basics.errors)
        details = AssetWizardDetailsForm(
            data={"identifier__Matrícula": "0928LXZ", "asset_data__marca": "Seat"},
            asset_type=self.asset_type,
        )
        self.assertTrue(details.is_valid(), details.errors)
        return view.done([basics, details])

    def test_done_creates_asset_with_identifiers_and_data(self):
        response = self._run_done()
        asset = Asset.objects.get(name="Wizard Test Arona")
        self.assertEqual(asset.client, self.client_obj)
        self.assertEqual(asset.data["marca"], "Seat")
        self.assertEqual(
            [(i.label, i.value) for i in asset.identifiers.all()],
            [("Matrícula", "0928LXZ")],
        )
        self.assertIn(f"/asset/{asset.pk}/change/", response.url)

    def test_done_in_popup_renders_popup_response(self):
        response = self._run_done(popup=True)
        asset = Asset.objects.get(name="Wizard Test Arona")
        self.assertEqual(response.template_name, "admin/popup_response.html")
        self.assertIn(str(asset.pk), response.context_data["popup_response_data"])
