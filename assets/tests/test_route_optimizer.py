"""
assets/tests/test_route_optimizer.py

Cheap tests for GoogleRouteOptimizer.get_distance_km() and the
AssetTransaction.distance_km property built on top of it.

Run just this file:
    python manage.py test assets.tests.test_route_optimizer
"""
from unittest.mock import patch

from django.test import TestCase, override_settings

from api.fleet.tests.helpers import make_company, make_zone
from assets.models import AssetTransaction, ZoneDistanceCache
from assets.services.route_optimizer import GoogleRouteOptimizer


class GetDistanceKmTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = make_company()
        cls.origin = make_zone(cls.company, name="Origin")
        cls.destination = make_zone(cls.company, name="Destination", lat=40.5, lon=-3.8)

    def test_missing_zone_returns_none(self):
        self.assertIsNone(GoogleRouteOptimizer().get_distance_km(None, self.destination))
        self.assertIsNone(GoogleRouteOptimizer().get_distance_km(self.origin, None))

    def test_same_zone_is_zero_without_calling_provider(self):
        with patch("assets.services.route_optimizer.GoogleRouteOptimizer._fetch_pairs") as mock_fetch:
            self.assertEqual(GoogleRouteOptimizer().get_distance_km(self.origin, self.origin), 0.0)
        mock_fetch.assert_not_called()

    def test_uses_cache_without_calling_provider(self):
        ZoneDistanceCache.objects.create(
            from_zone=self.origin, to_zone=self.destination,
            distance_meters=12_340, duration_seconds=900,
        )
        with patch("assets.services.route_optimizer.GoogleRouteOptimizer._fetch_pairs") as mock_fetch:
            distance = GoogleRouteOptimizer().get_distance_km(self.origin, self.destination)
        mock_fetch.assert_not_called()
        self.assertEqual(distance, 12.3)

    @patch("assets.services.route_optimizer.GoogleRouteOptimizer._fetch_pairs")
    def test_falls_back_to_provider_when_not_cached(self, mock_fetch):
        mock_fetch.return_value = {(self.origin.pk, self.destination.pk): (5_500, 400)}
        distance = GoogleRouteOptimizer().get_distance_km(self.origin, self.destination)
        self.assertEqual(distance, 5.5)
        # Only the one requested direction -- not the full cross product
        # (that's exactly the bug get_distances_km exists to avoid).
        mock_fetch.assert_called_once_with([(self.origin, self.destination)])

    @patch("assets.services.route_optimizer.GoogleRouteOptimizer._fetch_pairs", return_value={})
    def test_unresolvable_pair_returns_none_not_zero(self, mock_fetch):
        """A provider error or missing coordinates must not look like a real 0km trip."""
        self.assertIsNone(GoogleRouteOptimizer().get_distance_km(self.origin, self.destination))


class GetDistancesKmTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = make_company()
        cls.zone_a = make_zone(cls.company, name="A")
        cls.zone_b = make_zone(cls.company, name="B", lat=40.5, lon=-3.8)
        cls.zone_c = make_zone(cls.company, name="C", lat=40.6, lon=-3.9)

    def test_empty_input_returns_empty(self):
        self.assertEqual(GoogleRouteOptimizer().get_distances_km([]), {})

    def test_resolves_only_the_requested_pairs_not_every_combination(self):
        """3 zones could form 6 ordered pairs -- only the 2 actually asked
        for must be requested from the provider."""
        with patch(
            "assets.services.route_optimizer.GoogleRouteOptimizer._fetch_pairs",
            return_value={},
        ) as mock_fetch:
            GoogleRouteOptimizer().get_distances_km([
                (self.zone_a, self.zone_b),
                (self.zone_b, self.zone_c),
            ])
        (called_pairs,), _ = mock_fetch.call_args
        self.assertEqual(len(called_pairs), 2)

    def test_mixes_same_zone_cached_and_missing_pairs(self):
        ZoneDistanceCache.objects.create(
            from_zone=self.zone_a, to_zone=self.zone_b,
            distance_meters=7_000, duration_seconds=400,
        )
        with patch(
            "assets.services.route_optimizer.GoogleRouteOptimizer._fetch_pairs",
            return_value={(self.zone_b.pk, self.zone_c.pk): (3_000, 200)},
        ):
            result = GoogleRouteOptimizer().get_distances_km([
                (self.zone_a, self.zone_a),  # same zone -> 0, no lookup needed
                (self.zone_a, self.zone_b),  # cached
                (self.zone_b, self.zone_c),  # falls back to provider
            ])
        self.assertEqual(result[(self.zone_a.pk, self.zone_a.pk)], 0.0)
        self.assertEqual(result[(self.zone_a.pk, self.zone_b.pk)], 7.0)
        self.assertEqual(result[(self.zone_b.pk, self.zone_c.pk)], 3.0)


class AssetTransactionDistanceKmTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = make_company()
        cls.origin = make_zone(cls.company, name="Origin")
        cls.destination = make_zone(cls.company, name="Destination", lat=40.5, lon=-3.8)

    def test_delegates_to_optimizer(self):
        ZoneDistanceCache.objects.create(
            from_zone=self.origin, to_zone=self.destination,
            distance_meters=8_000, duration_seconds=600,
        )
        transaction = AssetTransaction(company=self.company, from_zone=self.origin, to_zone=self.destination)
        self.assertEqual(transaction.distance_km, 8.0)

    def test_missing_zone_is_none(self):
        transaction = AssetTransaction(company=self.company, from_zone=None, to_zone=self.destination)
        self.assertIsNone(transaction.distance_km)

    @patch(
        "assets.services.route_optimizer.GoogleRouteOptimizer.get_distance_km",
        side_effect=RuntimeError("boom"),
    )
    def test_provider_failure_never_raises(self, mock_get_distance_km):
        """This is a read-only add-on: an unexpected failure must degrade to
        None, never bubble up and break a page showing the transaction."""
        transaction = AssetTransaction(company=self.company, from_zone=self.origin, to_zone=self.destination)
        self.assertIsNone(transaction.distance_km)


class RouteNotFoundTests(TestCase):
    """ROUTE_NOT_FOUND (no road connects the two points) must leave the pair
    unresolved, not cache/return a fake 0km trip."""

    @classmethod
    def setUpTestData(cls):
        cls.company = make_company()
        cls.origin = make_zone(cls.company, name="Origin")
        cls.destination = make_zone(cls.company, name="Destination", lat=40.5, lon=-3.8)

    @override_settings(GOOGLE_MAPS_API_KEY="fake-key")
    @patch("assets.services.route_optimizer.requests.post")
    def test_route_not_found_is_not_cached_and_returns_none(self, mock_post):
        mock_post.return_value.json.return_value = [
            {"originIndex": 0, "destinationIndex": 0, "condition": "ROUTE_NOT_FOUND"},
        ]
        mock_post.return_value.raise_for_status.return_value = None

        distance = GoogleRouteOptimizer().get_distance_km(self.origin, self.destination)

        self.assertIsNone(distance)
        self.assertFalse(ZoneDistanceCache.objects.filter(from_zone=self.origin, to_zone=self.destination).exists())
