"""
api/fleet/tests/test_identifiers.py

GET /en/api/identifiers/?label=MATRICULA&asset_type=CAMION

Run just this file:
    python manage.py test api.fleet.tests.test_identifiers
"""

from django.test import TestCase

from assets.models import Asset, AssetIdentifier, AssetType
from company.models import Company
from api.fleet.tests.helpers import ApiFixtureMixin


class AssetIdentifierTests(ApiFixtureMixin, TestCase):
    """
    Tests for GET /en/api/identifiers/?label=MATRICULA&asset_type=CAMION
    These confirm the matricula dropdown and its autofill work correctly.
    """

    def setUp(self):
        super().setUp()
        self.company.asset_identifier_labels = ["MATRICULA"]
        self.company.save()

        self.camion_type = AssetType.objects.create(
            company=self.company,
            name="CAMION",
        )
        self.camion = Asset.objects.create(
            company=self.company,
            asset_type=self.camion_type,
            name="Camion 01",
            data={"tipo_cuba": "6m3", "implemento": "Gancho"},
        )
        AssetIdentifier.objects.create(
            asset=self.camion,
            label="MATRICULA",
            value="1234ABC",
        )

    def _get_identifiers(self, label="MATRICULA", asset_type="CAMION"):
        return self.api_client.get(
            f"/en/api/identifiers/?label={label}&asset_type={asset_type}"
        )

    def test_matricula_list_returns_identifier_value_and_flexible_data(self):
        """
        GET /identifiers/ must return this company's camion with the saved
        matricula value and the asset's flexible data (tipo_cuba, implemento)
        so the app can autofill those fields.
        """
        response = self._get_identifiers()
        self.assertEqual(response.status_code, 200)
        assets = response.json()["assets"]
        self.assertEqual(len(assets), 1)
        matricula = next(i for i in assets[0]["identifiers"] if i["label"] == "MATRICULA")
        self.assertEqual(matricula["value"], "1234ABC")
        asset_data = assets[0].get("data", {})
        self.assertEqual(asset_data.get("tipo_cuba"), "6m3")
        self.assertEqual(asset_data.get("implemento"), "Gancho")

    def test_identifier_from_other_company_not_returned(self):
        """
        A matricula belonging to a different company must not appear
        in the results — data isolation between companies.
        """
        other_company = Company.objects.create(name="Other Company")
        other_type = AssetType.objects.create(company=other_company, name="CAMION")
        other_asset = Asset.objects.create(
            company=other_company,
            asset_type=other_type,
            name="Camion 99",
        )
        AssetIdentifier.objects.create(
            asset=other_asset,
            label="MATRICULA",
            value="9999ZZZ",
        )
        assets = self._get_identifiers().json()["assets"]
        self.assertEqual(len(assets), 1)
        self.assertEqual(assets[0]["name"], "Camion 01")
