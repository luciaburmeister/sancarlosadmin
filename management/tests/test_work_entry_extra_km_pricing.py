"""extra_km (ProjectFile.extra_travel_km) exposed as a WorkEntryType pricing variable."""
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase

from company.models import Company, WorkingZone
from management.models import ProjectFile
from management.services.work_entry import WorkEntryService
from management.tests.test_entries import EntryFixtureMixin


class ExtraKmPricingTests(EntryFixtureMixin, TestCase):
    def test_extra_km_is_zero_without_home_base(self):
        context = WorkEntryService.build_full_context(
            self.revenue_type, self.source, Decimal("1"), project_file=self.project_file,
        )
        self.assertEqual(context["extra_km"], 0.0)

    def test_extra_km_feeds_a_pricing_step(self):
        home_base = WorkingZone.objects.create(
            company=self.company, name="San Carlos", zone_type=WorkingZone.ZoneType.ALMACEN, city="Málaga",
        )
        Company.objects.filter(pk=self.company.pk).update(home_base_zone=home_base)
        WorkingZone.objects.create(
            company=self.company, name="Obra", zone_type=WorkingZone.ZoneType.OBRA,
            city="Sevilla", project_file=self.project_file,
        )
        km_type = self._make_type("Kilometraje", self.revenue_type.direction, "extra_km * 3.5")
        # Uncached `company`, so it sees the home_base_zone just set above.
        fresh_project_file = ProjectFile.objects.select_related("company").get(pk=self.project_file.pk)

        with patch(
            "assets.services.route_optimizer.GoogleRouteOptimizer.get_distance_km",
            return_value=200.0,
        ):
            entry = WorkEntryService.create_entry_from_source(
                km_type, self.source, fresh_project_file, quantity=Decimal("1"),
            )

        self.assertEqual(entry.unit_price.amount, Decimal("700.00"))
