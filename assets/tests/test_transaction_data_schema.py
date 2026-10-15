"""
assets/tests/test_transaction_data_schema.py

Covers assets.transaction_data_schema -- the single place that maps typed
admin/wizard form fields to and from AssetTransaction.data's raw JSON keys.
Key-name fidelity matters here: management.services.work_entry reads these
exact keys (tipo_cuba, material_vertido, etc.) for pricing-rule matching.
"""
import datetime

from django import forms
from django.test import TestCase

from api.fleet.tests.helpers import make_asset, make_asset_type, make_company, make_zone
from assets.models import TransactionTypeChoices
from assets.transaction_data_schema import (
    SCHEMAS,
    TRANSACTION_DATA_CONDITIONAL_FIELDS,
    build_data_from_cleaned,
    data_schema_fields,
    initial_from_data,
)
from company.models import ConfigurationList, WorkingZone


class ConditionalFieldsVisibilityTests(TestCase):
    """TRANSACTION_DATA_CONDITIONAL_FIELDS drives the admin's show/hide JS
    (Unfold's conditional_fields). A field name shared by more than one
    schema (e.g. "matricula": Cuba+Porte) must stay visible for EVERY family
    that defines it, not just the last one built -- regression test for
    exactly that: a plain dict comprehension silently let the last schema
    clobber the others, hiding "matricula" on cuba movement/delivery.

    (reference_number used to be one of these shared fields too -- it's a
    real AssetTransaction model field now, always visible regardless of
    transaction_type, so it no longer appears in any schema here at all.)"""

    def test_every_schema_defining_a_field_keeps_it_visible_for_its_own_types(self):
        """General regression guard, not just the two fields known to have
        been broken: for every (schema, field) pair, the merged condition
        must still mention every one of that schema's own transaction_types
        -- proving no schema's contribution got clobbered by another's."""
        for schema in SCHEMAS:
            for field_name in schema.field_names:
                condition = TRANSACTION_DATA_CONDITIONAL_FIELDS[field_name]
                for transaction_type in schema.transaction_types:
                    self.assertIn(
                        f"transaction_type == '{transaction_type}'", condition,
                        f"{field_name!r}'s condition is missing {transaction_type!r} "
                        f"(from {schema.__name__}): {condition!r}",
                    )

    def test_reference_number_is_no_longer_a_schema_field(self):
        """It's a real model field now -- always visible, not conditional on
        transaction_type, so it must not appear in this dict at all."""
        self.assertNotIn("reference_number", TRANSACTION_DATA_CONDITIONAL_FIELDS)

    def test_a_single_schema_field_is_unaffected(self):
        """tipo_cuba only exists on CubaSchema -- sanity check the merge logic
        doesn't change behaviour for the non-shared common case."""
        condition = TRANSACTION_DATA_CONDITIONAL_FIELDS["tipo_cuba"]
        self.assertEqual(condition, "transaction_type == 'movement' || transaction_type == 'delivery'")


class CubaFamilySchemaTests(TestCase):
    def setUp(self):
        self.company = make_company()

    def test_config_backed_field_is_a_choicefield_when_config_row_exists(self):
        ConfigurationList.objects.create(company=self.company, key="materiales", values=["Tierras", "Agua"])
        fields = data_schema_fields(TransactionTypeChoices.MOVEMENT, self.company)
        self.assertIsInstance(fields["tipo_cuba"], forms.ChoiceField)
        self.assertIn(("Tierras", "Tierras"), fields["tipo_cuba"].choices)

    def test_config_backed_field_falls_back_to_charfield_without_config_row(self):
        fields = data_schema_fields(TransactionTypeChoices.MOVEMENT, self.company)
        self.assertIsInstance(fields["implemento"], forms.CharField)

    def test_vertedero_field_is_a_zone_choicefield_scoped_to_vertedero_zones(self):
        vertedero_zone = make_zone(self.company, name="Vertedero Sur")
        vertedero_zone.zone_type = WorkingZone.ZoneType.VERTEDERO
        vertedero_zone.save(update_fields=["zone_type"])
        obra_zone = make_zone(self.company, name="Obra Norte")
        obra_zone.zone_type = WorkingZone.ZoneType.OBRA
        obra_zone.save(update_fields=["zone_type"])

        fields = data_schema_fields(TransactionTypeChoices.MOVEMENT, self.company)
        qs = fields["vertedero"].queryset
        self.assertIn(vertedero_zone, qs)
        self.assertNotIn(obra_zone, qs)

    def test_initial_from_data_matches_vertedero_by_name(self):
        vertedero_zone = make_zone(self.company, name="Reciclados San Carlos")
        vertedero_zone.zone_type = WorkingZone.ZoneType.VERTEDERO
        vertedero_zone.save(update_fields=["zone_type"])

        initial = initial_from_data(
            TransactionTypeChoices.MOVEMENT,
            {"tipo_cuba": "Tierras", "vertedero": "Reciclados San Carlos"},
            company=self.company,
        )
        self.assertEqual(initial["tipo_cuba"], "Tierras")
        self.assertEqual(initial["vertedero"], vertedero_zone.pk)

    def test_build_data_from_cleaned_stores_vertedero_as_name_not_pk(self):
        vertedero_zone = make_zone(self.company, name="Reciclados San Carlos")
        vertedero_zone.zone_type = WorkingZone.ZoneType.VERTEDERO
        vertedero_zone.save(update_fields=["zone_type"])

        data = build_data_from_cleaned(
            TransactionTypeChoices.MOVEMENT,
            {"tipo_cuba": "Tierras", "vertedero": vertedero_zone, "cubaEstado": "Cuba llena"},
        )
        self.assertEqual(data["vertedero"], "Reciclados San Carlos")
        self.assertEqual(data["tipo_cuba"], "Tierras")
        self.assertEqual(data["cubaEstado"], "Cuba llena")

    def test_build_data_from_cleaned_preserves_unknown_existing_keys(self):
        data = build_data_from_cleaned(
            TransactionTypeChoices.MOVEMENT,
            {"tipo_cuba": "Tierras"},
            existing_data={"legacy_key": "keep me", "tipo_cuba": "old value"},
        )
        self.assertEqual(data["legacy_key"], "keep me")
        self.assertEqual(data["tipo_cuba"], "Tierras")


class PorteFamilySchemaTests(TestCase):
    def setUp(self):
        self.company = make_company()

    def test_build_data_from_cleaned_sets_movement_type_and_ids(self):
        data = build_data_from_cleaned(
            TransactionTypeChoices.PORTE_LOAD,
            {"matricula": "0709CBB", "load_description": "zahorra"},
        )
        self.assertEqual(data["movement_type"], "porte")
        self.assertEqual(data["action"], "carga")
        self.assertEqual(data["matricula"], "0709CBB")
        self.assertEqual(data["load_description"], "zahorra")

    def test_action_is_always_derived_from_transaction_type_not_cleaned(self):
        # "action" isn't a real field on the form (see PORTE_FIELD_NAMES) --
        # even if something stray ends up in cleaned["action"], the real
        # transaction_type -> action mapping always wins.
        cases = [
            (TransactionTypeChoices.PORTE_MOVEMENT, "recogida"),
            (TransactionTypeChoices.PORTE_LOAD, "carga"),
            (TransactionTypeChoices.PORTE_DELIVERY, "entrega"),
        ]
        for transaction_type, expected_action in cases:
            data = build_data_from_cleaned(transaction_type, {"action": "bogus"})
            self.assertEqual(data["action"], expected_action)

    def test_machine_field_renders_only_the_selected_asset(self):
        # Regression: the admin change page built an <option> (plus an
        # asset_type query) for every company asset, even when hidden.
        asset_type = make_asset_type(self.company)
        assets = [make_asset(self.company, asset_type, name=f"CUBA-{i:03}") for i in range(5)]
        field = data_schema_fields(TransactionTypeChoices.PORTE_LOAD, self.company)["machine_id"]

        with self.assertNumQueries(1):
            html = field.widget.render("machine_id", assets[2].pk)

        self.assertIn(f'value="{assets[2].pk}"', html)
        self.assertNotIn(f'value="{assets[0].pk}"', html)
        self.assertIn("data-ajax--url", html)

    def test_machine_field_still_validates_against_the_company(self):
        other = make_company(name="Other Co")
        foreign = make_asset(other, make_asset_type(other), name="FOREIGN")
        field = data_schema_fields(TransactionTypeChoices.PORTE_LOAD, self.company)["machine_id"]

        with self.assertRaises(forms.ValidationError):
            field.clean(foreign.pk)

    def test_unmapped_transaction_type_has_no_schema(self):
        self.assertEqual(data_schema_fields(TransactionTypeChoices.RETURN, self.company), {})
        self.assertEqual(build_data_from_cleaned(TransactionTypeChoices.RETURN, {"action": "carga"}), {})


class ReceiptFamilySchemaTests(TestCase):
    def setUp(self):
        self.company = make_company()

    def test_material_vertido_is_a_choicefield_when_config_row_exists(self):
        ConfigurationList.objects.create(company=self.company, key="material_vertido", values=["Tierras", "Hormigón"])
        fields = data_schema_fields(TransactionTypeChoices.RECEIPT, self.company)
        self.assertIsInstance(fields["material_vertido"], forms.ChoiceField)
        self.assertIn(("Tierras", "Tierras"), fields["material_vertido"].choices)

    def test_build_data_from_cleaned_keeps_receipt_keys(self):
        data = build_data_from_cleaned(
            TransactionTypeChoices.RECEIPT,
            {"nro_albaran": "01083485", "material_vertido": "Hormigón"},
        )
        self.assertEqual(data, {"nro_albaran": "01083485", "material_vertido": "Hormigón"})

    def test_initial_from_data_round_trips(self):
        initial = initial_from_data(
            TransactionTypeChoices.RECEIPT,
            {"nro_albaran": "01083485", "material_vertido": "Hormigón"},
        )
        self.assertEqual(initial["nro_albaran"], "01083485")
        self.assertEqual(initial["material_vertido"], "Hormigón")


class HoursFamilySchemaTests(TestCase):
    def test_build_data_from_cleaned_assembles_intervals_and_start_end(self):
        cleaned = {
            "interval_1_start": datetime.time(8, 0),
            "interval_1_end": datetime.time(12, 0),
            "interval_2_start": datetime.time(13, 0),
            "interval_2_end": datetime.time(17, 0),
            "interval_3_start": None,
            "interval_3_end": None,
            "interval_4_start": None,
            "interval_4_end": None,
        }
        tx_date = datetime.datetime(2026, 9, 17, 9, 0)
        data = build_data_from_cleaned(TransactionTypeChoices.MACHINE_HOURS, cleaned, tx_date=tx_date)

        self.assertEqual(
            data["intervals"],
            [{"start": "08:00:00", "end": "12:00:00"}, {"start": "13:00:00", "end": "17:00:00"}],
        )
        self.assertEqual(data["machine_start_time"], "2026-09-17T08:00:00")
        self.assertEqual(data["machine_end_time"], "2026-09-17T17:00:00")

    def test_open_last_interval_leaves_end_time_unset(self):
        cleaned = {
            "interval_1_start": datetime.time(8, 0),
            "interval_1_end": None,
            "interval_2_start": None,
            "interval_2_end": None,
            "interval_3_start": None,
            "interval_3_end": None,
            "interval_4_start": None,
            "interval_4_end": None,
        }
        data = build_data_from_cleaned(TransactionTypeChoices.LABORER_HOURS, cleaned)
        self.assertEqual(data["intervals"], [{"start": "08:00:00", "end": None}])
        self.assertIn("laborer_start_time", data)
        self.assertNotIn("laborer_end_time", data)

    def test_initial_from_data_round_trips_intervals(self):
        data = {"intervals": [{"start": "08:00:00", "end": "12:00:00"}]}
        initial = initial_from_data(TransactionTypeChoices.MACHINE_HOURS, data)
        self.assertEqual(initial["interval_1_start"], datetime.time(8, 0))
        self.assertEqual(initial["interval_1_end"], datetime.time(12, 0))
        self.assertIsNone(initial["interval_2_start"])
