"""Tests for the ProjectFile ↔ Asset link and its per-company enforcement."""
from django.test import TestCase

from assets.models import Asset, AssetType
from company.models import Company
from management.forms import ProjectFileAdminForm
from management.models import ProjectFile
from sales.models import Client


class ProjectFileAdminFormAssetTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Taller Co")
        self.other_company = Company.objects.create(name="Other Co")
        self.client_obj = Client.objects.create(name="Acme", company=self.company)
        self.other_client = Client.objects.create(name="Beta", company=self.company)
        self.asset_type = AssetType.objects.create(name="Vehículo", company=self.company)
        self.asset = Asset.objects.create(
            name="Seat Arona",
            asset_type=self.asset_type,
            client=self.client_obj,
            company=self.company,
        )

    # Cached money fields are not shown in the admin fieldsets, but the plain
    # form (fields="__all__") requires them — provide neutral values.
    _money_defaults = {
        "total_amount_0": "0",
        "total_amount_1": "EUR",
        "total_invoiced_0": "0",
        "total_invoiced_1": "EUR",
        "percentage_invoiced": "0",
    }

    def _form(self, **overrides):
        data = {
            "name": "Reparación",
            "company": self.company.pk,
            "client": self.client_obj.pk,
            "status": "new",
            "start_date": "2026-07-16",
            **self._money_defaults,
            **overrides,
        }
        return ProjectFileAdminForm(data=data)

    def test_asset_optional_when_company_does_not_require_it(self):
        form = self._form()
        self.assertTrue(form.is_valid(), form.errors)

    def test_asset_required_on_new_project_file_when_company_requires_it(self):
        self.company.require_asset_on_project_file = True
        self.company.save(update_fields=["require_asset_on_project_file"])
        form = self._form()
        self.assertFalse(form.is_valid())
        self.assertIn("asset", form.errors)

    def test_asset_requirement_satisfied(self):
        self.company.require_asset_on_project_file = True
        self.company.save(update_fields=["require_asset_on_project_file"])
        form = self._form(asset=self.asset.pk)
        self.assertTrue(form.is_valid(), form.errors)

    def test_existing_project_file_not_blocked_by_requirement(self):
        pf = ProjectFile.objects.create(
            name="Histórico", company=self.company, client=self.client_obj
        )
        self.company.require_asset_on_project_file = True
        self.company.save(update_fields=["require_asset_on_project_file"])
        form = ProjectFileAdminForm(
            data={
                "name": "Histórico editado",
                "company": self.company.pk,
                "client": self.client_obj.pk,
                "status": "active",
                "start_date": "2026-07-16",
                **self._money_defaults,
            },
            instance=pf,
        )
        self.assertTrue(form.is_valid(), form.errors)

    def test_asset_of_another_client_rejected(self):
        form = self._form(client=self.other_client.pk, asset=self.asset.pk)
        self.assertFalse(form.is_valid())
        self.assertIn("asset", form.errors)

    def test_asset_of_another_company_rejected(self):
        foreign_asset = Asset.objects.create(
            name="Ajeno",
            asset_type=AssetType.objects.create(name="Vehículo", company=self.other_company),
            company=self.other_company,
        )
        form = self._form(asset=foreign_asset.pk)
        self.assertFalse(form.is_valid())
        self.assertIn("asset", form.errors)

    def test_orphan_asset_allowed(self):
        orphan = Asset.objects.create(
            name="Sin dueño", asset_type=self.asset_type, company=self.company
        )
        form = self._form(asset=orphan.pk)
        self.assertTrue(form.is_valid(), form.errors)
