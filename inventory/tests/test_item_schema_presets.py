"""
inventory/tests/test_item_schema_presets.py

The seeded Shopify block, wine block and 'Vino' product schema.

Run:
    python manage.py test inventory.tests.test_item_schema_presets
"""
from io import StringIO

from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.test import TestCase

from company.models import Company
from inventory.choices import ItemSchemaKind
from inventory.models import ItemSchema, Warehouse
from inventory.services.item_schema_presets import (
    SHOPIFY_BLOCK,
    WINE_BLOCK,
    WINE_PRODUCT,
    seed_item_schemas,
)
from inventory.tests.factories import make_item, make_item_schema


class SeedItemSchemasTest(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name="Bodega Co")

    def test_creates_the_blocks_and_the_wine_product(self):
        result = seed_item_schemas(self.company)
        self.assertEqual(result.created_schemas, [SHOPIFY_BLOCK, WINE_BLOCK, WINE_PRODUCT])

        wine = ItemSchema.objects.get(company=self.company, name=WINE_PRODUCT)
        self.assertEqual(wine.kind, ItemSchemaKind.PRODUCT)
        self.assertEqual({s.name for s in wine.includes.all()}, {SHOPIFY_BLOCK, WINE_BLOCK})
        keys = [f.key for f in wine.effective_fields()]
        self.assertIn("shopify_handle", keys)
        self.assertIn("vintage", keys)
        self.assertEqual(len(keys), len(set(keys)))

    def test_rerun_is_a_no_op_and_keeps_edits(self):
        seed_item_schemas(self.company)
        block = ItemSchema.objects.get(company=self.company, name=WINE_BLOCK)
        block.fields.filter(key="winery").update(label="Bodega elaboradora")
        block.fields.filter(key="awards").delete()

        result = seed_item_schemas(self.company)
        self.assertEqual(result.created_schemas, [])
        self.assertEqual(result.created_fields, [f"{WINE_BLOCK}.awards"])
        self.assertEqual(block.fields.get(key="winery").label, "Bodega elaboradora")

    def test_a_colliding_company_field_aborts_the_whole_seed(self):
        make_item_schema(company=self.company, name=WINE_PRODUCT, fields=[{"key": "vintage"}])
        with self.assertRaises(ValidationError):
            seed_item_schemas(self.company)
        self.assertEqual(ItemSchema.objects.filter(company=self.company).count(), 1)

    def test_a_wine_item_validates_against_it(self):
        seed_item_schemas(self.company)
        wine = ItemSchema.objects.get(company=self.company, name=WINE_PRODUCT)
        warehouse = Warehouse.objects.create(code="WH", name="Main", company=self.company)
        item = make_item(company=self.company, warehouse=warehouse, code="V-1", metadata_schema=wine, metadata={
            "wine_type": "Tinto", "vintage": 2020, "aging": "Crianza", "alcohol_pct": 14,
            "volume_ml": 750, "shopify_status": "active", "weight_kg": 1.3,
        })
        item.full_clean()

        item.metadata = {"vintage": 2020}  # wine_type is required
        with self.assertRaises(ValidationError):
            item.full_clean()

    def test_command(self):
        out = StringIO()
        call_command("seed_item_schemas", company=[self.company.pk], stdout=out)
        self.assertIn("3 schema(s)", out.getvalue())
        self.assertTrue(ItemSchema.objects.filter(company=self.company, name=WINE_PRODUCT).exists())
