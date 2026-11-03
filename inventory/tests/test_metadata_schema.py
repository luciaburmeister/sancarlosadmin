"""
inventory/tests/test_metadata_schema.py

Item metadata: ItemSchema composition (product schemas + reusable blocks),
validation of InventoryItem.metadata against it, the barcode field, the
admin form that edits metadata and the import-export columns.

Run:
    python manage.py test inventory.tests.test_metadata_schema
"""
import time

import tablib
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.forms import modelform_factory
from django.test import TestCase
from django.urls import reverse

from company.active_company import ActiveCompany
from company.models import Company
from inventory.admin import InventoryItemForm
from inventory.choices import ItemSchemaKind
from inventory.forms import build_item_metadata_form_fields
from inventory.models import InventoryItem, ItemSchemaField, Warehouse
from inventory.resources import InventoryItemResource
from inventory.tests.factories import make_item, make_item_schema, make_schema_field

VALID_EAN13 = "4006381333931"


class MetadataSchemaTestBase(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name="Test Co")
        cls.other_company = Company.objects.create(name="Other Co")
        cls.warehouse = Warehouse.objects.create(code="WH1", name="Main", company=cls.company)
        cls.shipping = make_item_schema(
            company=cls.company, name="Shipping", kind=ItemSchemaKind.BLOCK,
            fields=[{"key": "weight_kg", "type": "number", "unit": "kg", "min_value": 0}],
        )
        cls.shopify = make_item_schema(
            company=cls.company, name="Shopify", kind=ItemSchemaKind.BLOCK,
            fields=[{"key": "tags", "type": "multiselect", "options": ["pvc", "aluminio"]}],
        )
        cls.ventana = make_item_schema(
            company=cls.company, name="Ventana", includes=[cls.shipping, cls.shopify],
            fields=[
                {"key": "color", "type": "select", "options": ["RAL9016", "RAL7016"], "required": True},
                {"key": "installed_on", "type": "date"},
            ],
        )

    def item(self, code="I-1", **kwargs):
        return make_item(company=self.company, warehouse=self.warehouse, code=code, **kwargs)


class ItemSchemaCompositionTest(MetadataSchemaTestBase):

    def test_effective_fields_are_blocks_then_own(self):
        fields = self.ventana.effective_fields()
        self.assertEqual([f.key for f in fields], ["weight_kg", "tags", "color", "installed_on"])
        self.assertEqual([f.source for f in fields], [self.shipping, self.shopify, self.ventana, self.ventana])

    def test_prefetched_schemas_cost_no_query_per_item(self):
        for code in ("I-1", "I-2", "I-3"):
            self.item(code=code, metadata_schema=self.ventana)
        items = list(
            InventoryItem.objects.filter(company=self.company)
            .select_related("metadata_schema")
            .prefetch_related("metadata_schema__fields", "metadata_schema__includes__fields")
        )
        with self.assertNumQueries(0):
            keys = [[f.key for f in item.metadata_schema.effective_fields()] for item in items]
        self.assertEqual(keys, [["weight_kg", "tags", "color", "installed_on"]] * 3)

    def test_json_schema(self):
        schema = self.ventana.json_schema()
        self.assertEqual(schema["required"], ["color"])
        self.assertTrue(schema["additionalProperties"])
        self.assertEqual(schema["properties"]["weight_kg"]["minimum"], 0)
        self.assertEqual(schema["properties"]["weight_kg"]["x-unit"], "kg")
        self.assertEqual(schema["properties"]["tags"]["items"], {"enum": ["pvc", "aluminio"]})

    def test_two_blocks_with_the_same_key_collide(self):
        dup = make_item_schema(
            company=self.company, name="Dup", kind=ItemSchemaKind.BLOCK, fields=[{"key": "weight_kg"}],
        )
        with self.assertRaisesMessage(ValidationError, "weight_kg"):
            self.ventana.validate_structure(includes=[self.shipping, dup], own_keys=["color"])

    def test_own_field_cannot_redefine_a_block_key(self):
        with self.assertRaisesMessage(ValidationError, "weight_kg"):
            self.ventana.validate_structure(includes=[self.shipping], own_keys=["weight_kg"])

    def test_new_block_field_colliding_with_an_including_product_is_rejected(self):
        field = ItemSchemaField(schema=self.shopify, key="color", label="Color")
        with self.assertRaises(ValidationError) as ctx:
            field.full_clean()
        self.assertIn("key", ctx.exception.message_dict)

    def test_new_block_field_without_collision_is_fine(self):
        ItemSchemaField(schema=self.shopify, key="handle", label="Handle").full_clean()

    def test_block_cannot_include_and_only_blocks_are_included(self):
        with self.assertRaises(ValidationError):
            self.shipping.validate_structure(includes=[self.shopify], own_keys=[])
        other = make_item_schema(company=self.company, name="Perfil")
        with self.assertRaises(ValidationError):
            self.ventana.validate_structure(includes=[other], own_keys=[])

    def test_block_from_another_company_is_rejected(self):
        foreign = make_item_schema(company=self.other_company, name="Shipping", kind=ItemSchemaKind.BLOCK)
        with self.assertRaises(ValidationError):
            self.ventana.validate_structure(includes=[foreign], own_keys=[])

    def test_field_definition_rules(self):
        with self.assertRaises(ValidationError):
            ItemSchemaField(schema=self.ventana, key="finish", label="F", type="select").full_clean()
        with self.assertRaises(ValidationError):
            ItemSchemaField(schema=self.ventana, key="finish", label="F", type="text", min_value=1).full_clean()

    def test_key_is_normalized(self):
        field = make_schema_field(schema=self.ventana, key="Opening Type")
        self.assertEqual(field.key, "opening_type")


class ItemMetadataValidationTest(MetadataSchemaTestBase):

    def test_valid_metadata_passes_and_extra_keys_are_kept(self):
        item = self.item(metadata_schema=self.ventana, metadata={
            "color": "RAL9016", "weight_kg": 2.5, "tags": ["pvc"], "installed_on": "2026-10-01", "legacy": 1,
        })
        item.full_clean()
        item.refresh_from_db()
        self.assertEqual(item.metadata["legacy"], 1)

    def assert_metadata_error(self, metadata, fragment):
        item = self.item(metadata_schema=self.ventana, metadata=metadata)
        with self.assertRaises(ValidationError) as ctx:
            item.full_clean()
        messages = ctx.exception.message_dict["metadata"]
        self.assertTrue(any(fragment in m for m in messages), messages)

    def test_required_field(self):
        self.assert_metadata_error({}, "color:")

    def test_number_minimum(self):
        self.assert_metadata_error({"color": "RAL9016", "weight_kg": -1}, "weight_kg:")

    def test_select_enum(self):
        self.assert_metadata_error({"color": "pink"}, "color:")

    def test_multiselect_items(self):
        self.assert_metadata_error({"color": "RAL9016", "tags": ["wood"]}, "tags:")

    def test_date_format(self):
        self.assert_metadata_error({"color": "RAL9016", "installed_on": "yesterday"}, "installed_on:")

    def test_without_schema_anything_goes(self):
        self.item(metadata={"whatever": {"nested": True}}).full_clean()

    def test_block_and_foreign_schema_cannot_be_assigned(self):
        for schema in (self.shipping, make_item_schema(company=self.other_company, name="X")):
            item = self.item(code=f"I-{schema.pk}", metadata_schema=schema)
            with self.assertRaises(ValidationError) as ctx:
                item.full_clean()
            self.assertIn("metadata_schema", ctx.exception.message_dict)

    def test_with_metadata_filters_by_containment(self):
        match = self.item(code="A", metadata={"color": "RAL9016", "tags": ["pvc", "aluminio"]})
        self.item(code="B", metadata={"color": "RAL7016"})
        self.assertEqual(list(InventoryItem.objects.with_metadata(color="RAL9016")), [match])
        self.assertEqual(list(InventoryItem.objects.with_metadata(tags=["pvc"])), [match])


class BarcodeTest(MetadataSchemaTestBase):

    def test_valid_gtin_and_free_format(self):
        for code in (VALID_EAN13, "96385074", "ABC-123", "12345"):
            self.item(code=f"I-{code}", barcode=code).full_clean()

    def test_bad_check_digit(self):
        item = self.item(barcode="4006381333932")
        with self.assertRaises(ValidationError) as ctx:
            item.full_clean()
        self.assertIn("barcode", ctx.exception.message_dict)

    def test_whitespace_is_stripped(self):
        self.assertEqual(self.item(barcode=f"  {VALID_EAN13} ").barcode, VALID_EAN13)

    def test_unique_per_company_when_set(self):
        self.item(code="A", barcode=VALID_EAN13)
        self.item(code="B")
        self.item(code="C")  # several blank barcodes are fine
        make_item(company=self.other_company, warehouse=None, code="A", barcode=VALID_EAN13)
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.item(code="D", barcode=VALID_EAN13)


class InventoryItemFormMetadataTest(MetadataSchemaTestBase):

    def form_class(self, schema):
        dynamic = type("DynamicInventoryItemForm", (InventoryItemForm,), build_item_metadata_form_fields(schema))
        return modelform_factory(
            InventoryItem, form=dynamic, fields=["company", "item_type", "default_warehouse", "metadata_schema"],
        )

    def data(self, **meta):
        return {
            "company": self.company.pk,
            "item_type": "STOCKABLE",
            "default_warehouse": self.warehouse.pk,
            "metadata_schema": self.ventana.pk,
            **{f"meta__{k}": v for k, v in meta.items()},
        }

    def test_fields_follow_the_schema(self):
        form = self.form_class(self.ventana)()
        self.assertEqual(form.fields["meta__weight_kg"].label, "weight_kg (kg)")
        self.assertTrue(form.fields["meta__color"].required)
        self.assertEqual(form.fields["meta__weight_kg"].metadata_source, self.shipping)

    def test_save_merges_and_keeps_unknown_keys(self):
        item = self.item(metadata_schema=self.ventana, metadata={"color": "RAL7016", "weight_kg": 1, "legacy": "x"})
        form = self.form_class(self.ventana)(
            data=self.data(color="RAL9016", weight_kg="", tags=["pvc"], installed_on="2026-10-01"),
            instance=item,
        )
        self.assertTrue(form.is_valid(), form.errors)
        form.save()
        item.refresh_from_db()
        self.assertEqual(
            item.metadata,
            {"color": "RAL9016", "tags": ["pvc"], "installed_on": "2026-10-01", "legacy": "x"},
        )

    def test_initial_values_come_from_metadata(self):
        item = self.item(metadata_schema=self.ventana, metadata={"color": "RAL7016"})
        form = self.form_class(self.ventana)(instance=item)
        self.assertEqual(form.initial["meta__color"], "RAL7016")

    def test_errors_land_on_the_field(self):
        form = self.form_class(self.ventana)(data=self.data(color="RAL9016", weight_kg="-3"))
        self.assertFalse(form.is_valid())
        self.assertIn("meta__weight_kg", form.errors)
        self.assertNotIn("__all__", form.errors)


class InventoryItemResourceMetadataTest(MetadataSchemaTestBase):

    @staticmethod
    def _heading(field_name):
        return str(InventoryItemResource().fields[field_name].column_name)

    def test_round_trip(self):
        item = self.item(barcode=VALID_EAN13, metadata_schema=self.ventana, metadata={"color": "RAL9016"})
        data = InventoryItemResource().export(InventoryItem.objects.filter(pk=item.pk))
        row = dict(zip(data.headers, data[0]))
        self.assertEqual(row[self._heading("barcode")], VALID_EAN13)
        self.assertEqual(row[self._heading("metadata_schema")], "Ventana")

        dataset = tablib.Dataset(headers=[
            "id", "code", self._heading("company"), self._heading("metadata_schema"), self._heading("metadata"),
        ])
        dataset.append([item.pk, item.code, self.company.name, "Ventana", '{"color": "RAL7016", "weight_kg": 3}'])
        result = InventoryItemResource().import_data(dataset, raise_errors=True)
        self.assertFalse(result.has_errors())
        item.refresh_from_db()
        self.assertEqual(item.metadata, {"color": "RAL7016", "weight_kg": 3})

    def test_import_rejects_invalid_metadata(self):
        item = self.item(metadata_schema=self.ventana, metadata={"color": "RAL9016"})
        dataset = tablib.Dataset(headers=["id", "code", self._heading("metadata")])
        dataset.append([item.pk, item.code, '{"color": "pink"}'])
        result = InventoryItemResource().import_data(dataset)
        self.assertTrue(result.has_validation_errors())
        item.refresh_from_db()
        self.assertEqual(item.metadata, {"color": "RAL9016"})


class MetadataAdminTest(MetadataSchemaTestBase):

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.superuser = get_user_model().objects.create_superuser("admin", "a@example.com", "pw")

    def setUp(self):
        self.client.force_login(self.superuser)
        self.addCleanup(ActiveCompany.clear_current)
        # The switchable-company ids are cached across tests; a stale list
        # would drop this company from the session below.
        cache.delete(ActiveCompany.SUPERUSER_IDS_CACHE_KEY)
        # Otherwise the middleware defaults to the first company by name.
        session = self.client.session
        session[ActiveCompany.SESSION_KEY] = [self.company.pk]
        session[ActiveCompany.SESSION_SET_AT_KEY] = time.time()
        session.save()

    def test_item_change_page_shows_the_schema_fields(self):
        item = self.item(metadata_schema=self.ventana, metadata={"color": "RAL9016"})
        response = self.client.get(reverse("admin:inventory_inventoryitem_change", args=[item.pk]))
        self.assertEqual(response.status_code, 200)
        for name in ("meta__weight_kg", "meta__tags", "meta__color", "meta__installed_on"):
            self.assertContains(response, f'name="{name}"')

    def test_item_schema_change_page_lists_effective_fields(self):
        response = self.client.get(reverse("admin:inventory_itemschema_change", args=[self.ventana.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "<code>weight_kg</code>", html=False)

    def test_item_schema_admin_rejects_a_key_collision(self):
        response = self.client.post(reverse("admin:inventory_itemschema_change", args=[self.ventana.pk]), {
            "company": self.company.pk,
            "name": "Ventana",
            "kind": ItemSchemaKind.PRODUCT,
            "description": "",
            "includes": [self.shipping.pk, self.shopify.pk],
            "fields-TOTAL_FORMS": "1",
            "fields-INITIAL_FORMS": "0",
            "fields-MIN_NUM_FORMS": "0",
            "fields-MAX_NUM_FORMS": "1000",
            "fields-0-key": "weight_kg",
            "fields-0-label": "Weight",
            "fields-0-type": "number",
            "fields-0-options": "[]",
            "fields-0-order": "5",
        })
        self.assertEqual(response.status_code, 200)  # re-rendered with errors, not saved
        self.assertIn("weight_kg", str(response.context["inline_admin_formsets"][0].formset.non_form_errors()))
        self.assertFalse(self.ventana.fields.filter(key="weight_kg").exists())
