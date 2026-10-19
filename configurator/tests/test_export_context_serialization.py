"""
The export context is also serialized to JSON (context_json, for debugging and
the AI template assistant); related instances inside a model must not be
expanded, or a many-to-many cycle never finishes.
"""
import json

from django.test import TestCase

from assets.tests.factories import make_truck, make_truck_type
from company.models import Company
from configurator.services.export.service import CustomJSONEncoder


class ExportContextSerializationTests(TestCase):
    def test_many_to_many_cycle_serializes_as_pks(self):
        company = Company.objects.create(name="Test Co")
        truck_type = make_truck_type(company)
        truck = make_truck(company, name="Truck 1", asset_type=truck_type)
        other = make_truck(company, name="Truck 2", asset_type=truck_type)
        # Symmetrical: other.excluded_assets now holds truck as well.
        truck.excluded_assets.add(other)

        payload = json.loads(json.dumps({"asset": truck}, cls=CustomJSONEncoder))

        self.assertEqual(payload["asset"]["excluded_assets"], [other.pk])
        self.assertEqual(payload["asset"]["__str__"], str(truck))
