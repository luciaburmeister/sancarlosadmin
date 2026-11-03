"""Tests for ProjectFile.distance_summary() -- total km for an expediente's
AssetTransactions, flagging the ones that couldn't be resolved.

Run just this file:
    python manage.py test management.tests.test_project_file_distance_summary
"""
from unittest.mock import patch

from django.test import TestCase

from assets.models import AssetTransaction, TransactionTypeChoices, ZoneDistanceCache
from company.models import Company, WorkingZone
from management.models import ProjectFile
from sales.models import Client


class ProjectFileDistanceSummaryTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name="Taller Co")
        cls.client_obj = Client.objects.create(name="Acme", company=cls.company)
        cls.project_file = ProjectFile.objects.create(name="Expediente 1", company=cls.company, client=cls.client_obj)
        cls.zone_a = WorkingZone.objects.create(company=cls.company, name="Obra A", location="POINT(-3.70379 40.416775)")
        cls.zone_b = WorkingZone.objects.create(company=cls.company, name="Vertedero B", location="POINT(-3.8 40.5)")
        cls.zone_c = WorkingZone.objects.create(company=cls.company, name="Almacén C", location="POINT(-3.9 40.6)")

    def _make_transaction(self, from_zone=None, to_zone=None):
        return AssetTransaction.objects.create(
            company=self.company,
            project_file=self.project_file,
            transaction_type=TransactionTypeChoices.PORTE_DELIVERY,
            from_zone=from_zone,
            to_zone=to_zone,
        )

    def test_no_transactions_returns_zero(self):
        summary = self.project_file.distance_summary()
        self.assertEqual(summary.total_km, 0.0)
        self.assertEqual(summary.resolved_count, 0)
        self.assertEqual(summary.unresolved_transaction_ids, [])

    def test_transactions_without_both_zones_are_ignored_not_flagged(self):
        """A transaction with no zones at all isn't a distance error -- it's
        simply not a route-based transaction (e.g. machine hours)."""
        self._make_transaction(from_zone=None, to_zone=None)
        self._make_transaction(from_zone=self.zone_a, to_zone=None)

        summary = self.project_file.distance_summary()
        self.assertEqual(summary.resolved_count, 0)
        self.assertEqual(summary.unresolved_transaction_ids, [])

    def test_sums_resolved_distances_across_repeated_pairs(self):
        ZoneDistanceCache.objects.create(from_zone=self.zone_a, to_zone=self.zone_b, distance_meters=10_000, duration_seconds=600)
        ZoneDistanceCache.objects.create(from_zone=self.zone_b, to_zone=self.zone_a, distance_meters=10_000, duration_seconds=600)
        self._make_transaction(self.zone_a, self.zone_b)
        self._make_transaction(self.zone_a, self.zone_b)  # same pair again -- counts twice
        self._make_transaction(self.zone_b, self.zone_a)

        with patch("assets.services.route_optimizer.GoogleRouteOptimizer._fetch_pairs") as mock_fetch:
            summary = self.project_file.distance_summary()
        mock_fetch.assert_not_called()
        self.assertEqual(summary.resolved_count, 3)
        self.assertEqual(summary.total_km, 30.0)
        self.assertEqual(summary.unresolved_transaction_ids, [])

    @patch("assets.services.route_optimizer.GoogleRouteOptimizer._fetch_pairs", return_value={})
    def test_unresolved_pair_is_flagged_not_counted_as_zero(self, mock_fetch):
        ZoneDistanceCache.objects.create(from_zone=self.zone_a, to_zone=self.zone_b, distance_meters=5_000, duration_seconds=300)
        resolved = self._make_transaction(self.zone_a, self.zone_b)
        unresolved = self._make_transaction(self.zone_b, self.zone_c)  # not cached, provider returns nothing

        summary = self.project_file.distance_summary()

        self.assertEqual(summary.resolved_count, 1)
        self.assertEqual(summary.total_km, 5.0)
        self.assertEqual(summary.unresolved_transaction_ids, [unresolved.pk])
        self.assertNotIn(resolved.pk, summary.unresolved_transaction_ids)

    def test_only_fetches_distinct_zone_pairs_once(self):
        """Ten transactions between the same two zones must cost one provider
        round-trip for exactly that one pair, not ten, and not the reverse
        direction either since nothing ever travels b->a here."""
        self._make_transaction(self.zone_a, self.zone_b)
        for _ in range(9):
            self._make_transaction(self.zone_a, self.zone_b)

        with patch(
            "assets.services.route_optimizer.GoogleRouteOptimizer._fetch_pairs",
            return_value={(self.zone_a.pk, self.zone_b.pk): (4_000, 200)},
        ) as mock_fetch:
            summary = self.project_file.distance_summary()

        mock_fetch.assert_called_once_with([(self.zone_a, self.zone_b)])
        self.assertEqual(summary.resolved_count, 10)
        self.assertEqual(summary.total_km, 40.0)

    def test_does_not_resolve_the_full_cross_product_of_touched_zones(self):
        """Regression guard for a real bug: a project file touching many
        distinct zones must only ask the provider for the pairs that
        actually occur in its transactions, never every possible ordered
        pair between all of them -- that blew up to the thousands on a real
        expediente (53 zones -> 2756 possible pairs, 57 actually used) and
        fired a live Google call per unneeded pair, making the admin page
        take ages to render."""
        zone_d = WorkingZone.objects.create(company=self.company, name="Obra D", location="POINT(-4.0 40.7)")
        zone_e = WorkingZone.objects.create(company=self.company, name="Obra E", location="POINT(-4.1 40.8)")
        # 5 distinct zones, but only 2 distinct pairs ever actually travelled.
        self._make_transaction(self.zone_a, self.zone_b)
        self._make_transaction(self.zone_c, zone_d)
        self._make_transaction(zone_e, self.zone_a)  # shares zone_a with the first pair

        with patch(
            "assets.services.route_optimizer.GoogleRouteOptimizer._fetch_pairs",
            return_value={},
        ) as mock_fetch:
            self.project_file.distance_summary()

        (called_pairs,), _kwargs = mock_fetch.call_args
        self.assertEqual(len(called_pairs), 3)  # exactly the 3 pairs used, not 5*4=20

    @patch(
        "assets.services.route_optimizer.GoogleRouteOptimizer.get_distances_km",
        side_effect=RuntimeError("boom"),
    )
    def test_provider_failure_never_raises(self, mock_matrix):
        """Read-only aggregation: an unexpected failure must flag every
        transaction as unresolved, never break the page showing it."""
        t1 = self._make_transaction(self.zone_a, self.zone_b)
        t2 = self._make_transaction(self.zone_b, self.zone_c)

        summary = self.project_file.distance_summary()

        self.assertEqual(summary.total_km, 0.0)
        self.assertEqual(summary.resolved_count, 0)
        self.assertEqual(set(summary.unresolved_transaction_ids), {t1.pk, t2.pk})
