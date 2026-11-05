"""Tests for ProjectFile.extra_travel_km() -- billable distance from the
company's home base zone to a project file's own work site.

Run just this file:
    python manage.py test management.tests.test_project_file_extra_travel_km
"""
from unittest.mock import patch

from django.test import TestCase

from company.models import Company, WorkingZone
from management.models import ProjectFile
from sales.models import Client


class ProjectFileExtraTravelKmTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name="Taller Co")
        cls.client_obj = Client.objects.create(name="Acme", company=cls.company)
        cls.project_file = ProjectFile.objects.create(name="Expediente 1", company=cls.company, client=cls.client_obj)

    def _set_home_base(self, city="Málaga"):
        zone = WorkingZone.objects.create(
            company=self.company, name="San Carlos", zone_type=WorkingZone.ZoneType.ALMACEN, city=city,
        )
        # Updated at the DB layer, not on the shared cls.company instance, so
        # it can't leak into other test methods reusing that same instance.
        Company.objects.filter(pk=self.company.pk).update(home_base_zone=zone)
        return zone

    def _make_site(self, city):
        return WorkingZone.objects.create(
            company=self.company, name="Obra", zone_type=WorkingZone.ZoneType.OBRA,
            city=city, project_file=self.project_file,
        )

    def _fresh_project_file(self):
        """A ProjectFile instance with an uncached `company`, so it sees
        whatever home_base_zone the test just set at the DB layer."""
        return ProjectFile.objects.select_related("company").get(pk=self.project_file.pk)

    def test_no_home_base_returns_none(self):
        self._make_site("Sevilla")
        self.assertIsNone(self._fresh_project_file().extra_travel_km())

    def test_no_work_site_returns_none(self):
        self._set_home_base()
        self.assertIsNone(self._fresh_project_file().extra_travel_km())

    def test_same_city_as_home_base_not_billable(self):
        self._set_home_base(city="Málaga")
        self._make_site(city="málaga ")  # case/whitespace-insensitive match
        self.assertIsNone(self._fresh_project_file().extra_travel_km())

    def test_different_city_resolves_real_distance(self):
        home_base = self._set_home_base(city="Málaga")
        site = self._make_site(city="Sevilla")

        with patch(
            "assets.services.route_optimizer.GoogleRouteOptimizer.get_distance_km",
            return_value=217.3,
        ) as mock_get_distance:
            km = self._fresh_project_file().extra_travel_km()

        mock_get_distance.assert_called_once_with(home_base, site)
        self.assertEqual(km, 217.3)

    def test_provider_failure_returns_none_not_zero(self):
        self._set_home_base(city="Málaga")
        self._make_site(city="Sevilla")

        with patch(
            "assets.services.route_optimizer.GoogleRouteOptimizer.get_distance_km",
            side_effect=RuntimeError("boom"),
        ):
            self.assertIsNone(self._fresh_project_file().extra_travel_km())

    def test_ignores_inactive_or_non_obra_zones(self):
        self._set_home_base(city="Málaga")
        WorkingZone.objects.create(
            company=self.company, name="Almacén secundario", zone_type=WorkingZone.ZoneType.ALMACEN,
            city="Sevilla", project_file=self.project_file,
        )
        WorkingZone.objects.create(
            company=self.company, name="Obra inactiva", zone_type=WorkingZone.ZoneType.OBRA,
            city="Sevilla", project_file=self.project_file, is_active=False,
        )
        self.assertIsNone(self._fresh_project_file().extra_travel_km())
