"""
api/fleet/tests/test_identifiers.py

Tests 76-80: GET /en/api/identifiers/?label=MATRICULA&asset_type=CAMION

Run just this file:
    python manage.py test api.fleet.tests.test_identifiers
"""

from django.test import TestCase
from rest_framework.test import APIClient

from assets.models import Asset, AssetIdentifier, AssetType
from company.models import Company
from api.fleet.tests.helpers import make_company, make_user, make_employee


class AssetIdentifierTests(TestCase):
    """
    Tests for GET /en/api/identifiers/?label=MATRICULA&asset_type=CAMION
    These confirm the matricula dropdown and its autofill work correctly.
    """

    def setUp(self):
        self.api_client = APIClient()
        self.company = make_company()
        self.company.asset_identifier_labels = ["MATRICULA"]
        self.company.save()

        self.user = make_user()
        self.employee = make_employee(self.company, self.user)
        self.api_client.force_authenticate(user=self.user)

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

    # Test 76
    def test_matricula_list_returns_identifiers(self):
        """GET /identifiers/ must return the matricula for this company's camion."""
        response = self._get_identifiers()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()["assets"]), 1)

    # Test 77
    def test_matricula_identifier_value_is_correct(self):
        """The returned identifier value must match what was saved."""
        assets = self._get_identifiers().json()["assets"]
        identifiers = assets[0]["identifiers"]
        matricula = next(i for i in identifiers if i["label"] == "MATRICULA")
        self.assertEqual(matricula["value"], "1234ABC")

    # Test 78
    def test_identifier_includes_flexible_data(self):
        """
        The asset's flexible data (tipo_cuba, implemento) must be included
        in the response so the app can autofill those fields.
        """
        assets = self._get_identifiers().json()["assets"]
        asset_data = assets[0].get("data", {})
        self.assertEqual(asset_data.get("tipo_cuba"), "6m3")
        self.assertEqual(asset_data.get("implemento"), "Gancho")

    # Test 79
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

    # Test 80
    def test_unauthenticated_identifiers_request_is_rejected(self):
        """No token — identifiers endpoint must return 401."""
        self.api_client.force_authenticate(user=None)
        response = self._get_identifiers()
        self.assertEqual(response.status_code, 401)
