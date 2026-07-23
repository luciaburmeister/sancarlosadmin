"""Tests for AssetTypeField definitions and the dynamic asset form."""
from django import forms
from django.test import TestCase

from assets.forms import AssetForm, build_asset_data_form_fields, DATA_FIELD_PREFIX
from assets.models import Asset, AssetType, AssetTypeField
from company.models import Company
from sales.models import Client


class AssetTypeFieldTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Taller Co")
        self.asset_type = AssetType.objects.create(name="Vehículo", company=self.company)

    def test_name_normalized_to_snake_case(self):
        field = AssetTypeField.objects.create(
            asset_type=self.asset_type, name="Fecha Matriculacion", label="Fecha 1ª mat.",
        )
        self.assertEqual(field.name, "fecha_matriculacion")

    def test_build_form_fields_types_and_order(self):
        AssetTypeField.objects.create(
            asset_type=self.asset_type, name="marca", label="Marca", type="text", order=1,
        )
        AssetTypeField.objects.create(
            asset_type=self.asset_type, name="kilometros", label="Km", type="number", order=2,
        )
        AssetTypeField.objects.create(
            asset_type=self.asset_type, name="color", label="Color", type="select",
            options=["Rojo", "Azul"], order=3,
        )
        fields = build_asset_data_form_fields(self.asset_type)
        names = list(fields)
        self.assertEqual(
            names, ["asset_data__marca", "asset_data__kilometros", "asset_data__color"]
        )
        self.assertIsInstance(fields["asset_data__marca"], forms.CharField)
        self.assertIsInstance(fields["asset_data__kilometros"], forms.FloatField)
        self.assertIsInstance(fields["asset_data__color"], forms.ChoiceField)
        self.assertIn(("Rojo", "Rojo"), fields["asset_data__color"].choices)

    def test_schema_is_generated_from_fields(self):
        AssetTypeField.objects.create(
            asset_type=self.asset_type, name="marca", label="Marca", type="text", required=True, order=1,
        )
        AssetTypeField.objects.create(
            asset_type=self.asset_type, name="anio", label="Año", type="number", order=2,
        )
        AssetTypeField.objects.create(
            asset_type=self.asset_type, name="color", label="Color", type="select",
            options=["Rojo", "Azul"], order=3,
        )
        self.asset_type.refresh_from_db()

        schema = self.asset_type.schema
        self.assertEqual(schema["type"], "object")
        self.assertEqual(schema["properties"]["marca"]["type"], "string")
        self.assertEqual(schema["properties"]["anio"]["type"], "number")
        self.assertEqual(schema["properties"]["color"]["enum"], ["Rojo", "Azul"])
        self.assertEqual(schema["required"], ["marca"])
        self.assertTrue(schema["additionalProperties"])

    def test_schema_is_regenerated_when_field_deleted(self):
        field = AssetTypeField.objects.create(
            asset_type=self.asset_type, name="marca", label="Marca", type="text", required=True, order=1,
        )
        self.asset_type.refresh_from_db()
        self.assertIn("marca", self.asset_type.schema.get("properties", {}))

        field.delete()
        self.asset_type.refresh_from_db()
        self.assertEqual(self.asset_type.schema.get("properties"), {})

    def test_schema_generation_ignores_stale_prefetched_cache(self):
        cached_type = AssetType.objects.prefetch_related("fields").get(pk=self.asset_type.pk)
        # Prime an empty prefetched cache snapshot.
        self.assertEqual(list(cached_type.fields.all()), [])

        # Create through a different path (like inline saves can do).
        AssetTypeField.objects.create(
            asset_type=self.asset_type, name="matricula", label="Matrícula", type="text", required=True, order=1,
        )

        # Re-sync from the cached parent instance should still include new DB rows.
        cached_type.sync_schema_from_fields()
        cached_type.refresh_from_db()
        self.assertIn("matricula", cached_type.schema.get("properties", {}))


class AssetIdentifierLabelsTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(
            name="Taller Co", asset_identifier_labels=["N. Serie"]
        )
        self.client_obj = Client.objects.create(name="Acme", company=self.company)
        self.vehicle_type = AssetType.objects.create(
            name="Vehículo", company=self.company,
            identifier_labels=["Matrícula", "Bastidor"],
        )
        self.other_type = AssetType.objects.create(name="Cuba", company=self.company)

    def test_type_labels_take_precedence_over_company(self):
        self.assertEqual(
            self.vehicle_type.get_identifier_labels(), ["Matrícula", "Bastidor"]
        )

    def test_fallback_to_company_labels(self):
        self.assertEqual(self.other_type.get_identifier_labels(), ["N. Serie"])

    def test_clean_validates_against_type_labels(self):
        from django.core.exceptions import ValidationError
        from assets.models import AssetIdentifier

        asset = Asset.objects.create(
            name="Seat", asset_type=self.vehicle_type, company=self.company
        )
        ident = AssetIdentifier(asset=asset, label="N. Serie", value="X")
        with self.assertRaises(ValidationError):
            ident.full_clean()
        AssetIdentifier(asset=asset, label="Matrícula", value="0928LXZ").full_clean()


class AssetDynamicFormTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Taller Co")
        self.client_obj = Client.objects.create(name="Acme", company=self.company)
        self.asset_type = AssetType.objects.create(name="Vehículo", company=self.company)
        AssetTypeField.objects.create(
            asset_type=self.asset_type, name="marca", label="Marca",
            type="text", required=True, order=1,
        )
        AssetTypeField.objects.create(
            asset_type=self.asset_type, name="anio", label="Año",
            type="number", order=2,
        )
        self.asset = Asset.objects.create(
            name="Seat Arona",
            asset_type=self.asset_type,
            client=self.client_obj,
            company=self.company,
            data={"marca": "Seat", "legacy_key": "keep-me"},
        )
        self.form_class = type(
            "DynamicAssetForm",
            (AssetForm,),
            build_asset_data_form_fields(self.asset_type),
        )

    def _base_data(self, **extra):
        return {
            "name": self.asset.name,
            "asset_type": self.asset_type.pk,
            "client": self.client_obj.pk,
            "company": self.company.pk,
            "is_active": "on",
            "description": "",
            **extra,
        }

    def test_initial_comes_from_asset_data(self):
        form = self.form_class(instance=self.asset)
        self.assertEqual(form.initial[f"{DATA_FIELD_PREFIX}marca"], "Seat")

    def test_required_schema_field_enforced(self):
        form = self.form_class(
            data=self._base_data(asset_data__marca=""), instance=self.asset
        )
        self.assertFalse(form.is_valid())
        self.assertIn("asset_data__marca", form.errors)

    def test_save_merges_values_and_preserves_unknown_keys(self):
        form = self.form_class(
            data=self._base_data(asset_data__marca="Cupra", asset_data__anio="2022"),
            instance=self.asset,
        )
        self.assertTrue(form.is_valid(), form.errors)
        asset = form.save()
        asset.refresh_from_db()
        self.assertEqual(asset.data["marca"], "Cupra")
        self.assertEqual(asset.data["anio"], 2022.0)
        self.assertEqual(asset.data["legacy_key"], "keep-me")
