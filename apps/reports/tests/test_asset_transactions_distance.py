"""Tests for the distance_km measure on AssetTransactionsDataset -- lets the
AI agent answer "how many km" questions grouped by driver/asset/expediente.

Run just this file:
    python manage.py test apps.reports.tests.test_asset_transactions_distance
"""
from django.contrib.auth import get_user_model
from django.test import TestCase

from apps.reports.constants import ReportDomain
from apps.reports.services.compiler import ReportCompiler
from apps.reports.spec import ReportSpec
from assets.models import AssetTransaction, TransactionTypeChoices, ZoneDistanceCache
from company.active_company import ActiveCompany
from company.models import Company, CompanyUser, WorkingZone

User = get_user_model()


def _distance_spec(**overrides):
    data = {
        "dataset": ReportDomain.ASSET_TRANSACTIONS,
        "dimensions": ["driver"],
        "aggregations": [
            {"measure": "distance_km", "function": "sum", "alias": "total_km"},
        ],
        "filters": [],
        "parameters": [],
        "chart": "none",
        "chart_label_dimension": None,
        "chart_value_aggregation": None,
    }
    data.update(overrides)
    return ReportSpec.from_dict(data)


class AssetTransactionsDistanceMeasureTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name="Taller Co", tax_identifier="T1")
        cls.user = User.objects.create_user(username="reporter", password="pass", is_staff=True)
        CompanyUser.objects.create(user=cls.user, company=cls.company, is_active=True)
        cls.user.user_permissions.add(
            *_perm_objects("assets", "view_assettransaction")
        )

        cls.zone_a = WorkingZone.objects.create(company=cls.company, name="Obra A", location="POINT(-3.70379 40.416775)")
        cls.zone_b = WorkingZone.objects.create(company=cls.company, name="Vertedero B", location="POINT(-3.8 40.5)")
        ZoneDistanceCache.objects.create(
            from_zone=cls.zone_a, to_zone=cls.zone_b, distance_meters=12_000, duration_seconds=900,
        )

    def _make_transaction(self, from_zone=None, to_zone=None, performed_by=None):
        return AssetTransaction.objects.create(
            company=self.company,
            transaction_type=TransactionTypeChoices.PORTE_DELIVERY,
            from_zone=from_zone,
            to_zone=to_zone,
            performed_by=performed_by,
        )

    def test_sums_distance_for_a_cached_pair(self):
        ActiveCompany.set_current_ids([self.company.pk])
        self._make_transaction(self.zone_a, self.zone_b)
        self._make_transaction(self.zone_a, self.zone_b)

        result = ReportCompiler().compile(_distance_spec(dimensions=[]), self.user)

        self.assertEqual(result.rows[0]["total_km"], 24.0)

    def test_same_zone_counts_as_zero_not_excluded(self):
        ActiveCompany.set_current_ids([self.company.pk])
        self._make_transaction(self.zone_a, self.zone_a)

        result = ReportCompiler().compile(_distance_spec(dimensions=[]), self.user)

        self.assertEqual(result.rows[0]["total_km"], 0.0)

    def test_unresolved_pair_is_excluded_not_counted_as_zero(self):
        """A pair never resolved before must not silently drag the sum to 0
        -- it's simply absent from the total, same rule as distance_km."""
        ActiveCompany.set_current_ids([self.company.pk])
        zone_c = WorkingZone.objects.create(company=self.company, name="Almacén C", location="POINT(-3.9 40.6)")
        self._make_transaction(self.zone_a, self.zone_b)  # cached: 12km
        self._make_transaction(self.zone_b, zone_c)  # never resolved

        result = ReportCompiler().compile(_distance_spec(dimensions=[]), self.user)

        self.assertEqual(result.rows[0]["total_km"], 12.0)

    def test_distance_not_annotated_when_not_requested(self):
        """The Subquery join only runs when a report actually asks for
        distance_km -- cheap for every other asset-transactions report."""
        ActiveCompany.set_current_ids([self.company.pk])
        self._make_transaction(self.zone_a, self.zone_b)

        spec = ReportSpec.from_dict({
            "dataset": ReportDomain.ASSET_TRANSACTIONS,
            "dimensions": [],
            "aggregations": [{"measure": "*", "function": "count", "alias": "trip_count"}],
            "filters": [],
            "parameters": [],
            "chart": "none",
            "chart_label_dimension": None,
            "chart_value_aggregation": None,
        })
        result = ReportCompiler().compile(spec, self.user)

        self.assertEqual(result.rows[0]["trip_count"], 1)
        self.assertNotIn("total_km", result.rows[0])


def _perm_objects(app_label, *codenames):
    from django.contrib.auth.models import Permission

    return Permission.objects.filter(content_type__app_label=app_label, codename__in=codenames)
