"""
api/fleet/tests/test_zones.py

Tests 40-45: find_zones_within_radius GPS zone detection

Run just this file:
    python manage.py test api.fleet.tests.test_zones
"""

from django.test import TestCase

from api.fleet.utils import find_zones_within_radius
from api.fleet.tests.helpers import make_company, make_zone


class WorkingZoneDetectionTests(TestCase):
    """
    Tests for find_zones_within_radius — the logic that detects
    which zone the driver is at based on their GPS coordinates.
    """

    def setUp(self):
        self.company = make_company()

    # Test 40
    def test_single_nearby_zone_is_returned(self):
        """
        When the driver is within 250m of exactly one zone,
        that zone must be returned.
        """
        zone = make_zone(self.company, lat=40.416775, lon=-3.703790)
        results = find_zones_within_radius(40.416775, -3.703790, self.company)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0][0], zone)

    # Test 41
    def test_multiple_nearby_zones_are_all_returned(self):
        """
        When the driver is within 250m of two zones, both must be returned.
        This is what triggers the zone-picker dialog in the app.
        """
        zone1 = make_zone(self.company, name="Zona 1", lat=40.416775, lon=-3.703790)
        zone2 = make_zone(self.company, name="Zona 2", lat=40.417000, lon=-3.703800)

        results = find_zones_within_radius(40.416775, -3.703790, self.company)
        returned_zones = [r[0] for r in results]

        self.assertIn(zone1, returned_zones)
        self.assertIn(zone2, returned_zones)

    # Test 42
    def test_far_away_zone_is_not_returned(self):
        """
        A zone more than 250m away must not be returned.
        Driver is in Madrid center, zone is in Vallecas (~5km away).
        """
        make_zone(self.company, lat=40.385000, lon=-3.650000)
        results = find_zones_within_radius(40.416775, -3.703790, self.company)
        self.assertEqual(len(results), 0)

    # Test 43
    def test_zone_detection_with_no_zones_returns_empty_list(self):
        """
        When there are no zones in the database,
        the function must return an empty list — not crash.
        """
        results = find_zones_within_radius(40.416775, -3.703790, self.company)
        self.assertEqual(results, [])

    # Test 44
    def test_zones_are_sorted_by_distance_closest_first(self):
        """
        When multiple zones are returned, they must be ordered
        closest first so the dialog shows the most likely zone at the top.
        """
        zone_a = make_zone(self.company, name="Zona A", lat=40.416780, lon=-3.703790)
        zone_b = make_zone(self.company, name="Zona B", lat=40.417500, lon=-3.703790)

        results = find_zones_within_radius(40.416775, -3.703790, self.company)
        self.assertEqual(results[0][0], zone_a)
        self.assertEqual(results[1][0], zone_b)

    # Test 45
    def test_inactive_zone_is_not_returned(self):
        """
        A zone with is_active=False must be excluded even if within range.
        """
        zone = make_zone(self.company, lat=40.416775, lon=-3.703790)
        zone.is_active = False
        zone.save()

        results = find_zones_within_radius(40.416775, -3.703790, self.company)
        self.assertEqual(len(results), 0)
