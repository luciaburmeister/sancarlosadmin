"""
assets/tests/test_manual_transaction_form.py

Covers ManualTransactionForm's `asset` field -- the fix for the "Create
Transaction" wizard refusing to submit when the planning has no
asset/transport_asset/machine assigned (the mobile app's QR-scan assignment
step never ran against it), with no way to pick one in the form.
"""
from django.test import TestCase

from api.fleet.tests.helpers import make_asset, make_asset_type, make_company, make_employee, make_planning, make_user, make_zone
from assets.forms import ManualTransactionForm
from assets.models import (
    AssetIdentifier,
    AssetIdentifierLabel,
    AssetTypeCategory,
    CubaFillState,
    FormAffirmative,
    PlanningType,
    TransactionTypeChoices,
)
from company.models import WorkingZone


def truck_with_plate(company, plate="0709CBB", name="Truck-001"):
    truck_type = make_asset_type(company, name=f"Camion {plate}")
    truck_type.category = AssetTypeCategory.TRUCK
    truck_type.save(update_fields=["category"])
    truck = make_asset(company, truck_type, name=name)
    AssetIdentifier.objects.create(asset=truck, label=AssetIdentifierLabel.PLATE, value=plate)
    return truck


class ManualTransactionFormAssetFieldTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = make_company()
        cls.user = make_user()
        cls.employee = make_employee(cls.company, cls.user)
        cls.asset_type = make_asset_type(cls.company)

    def test_invalid_without_asset_when_planning_has_none(self):
        planning = make_planning(self.company, asset=None, assigned_to=self.employee)
        form = ManualTransactionForm(
            data={"transaction_type": TransactionTypeChoices.MOVEMENT},
            planning=planning,
        )
        self.assertFalse(form.is_valid())
        self.assertIn("asset", form.errors)

    def test_valid_when_an_asset_is_picked_for_an_assetless_planning(self):
        asset = make_asset(self.company, self.asset_type)
        truck = truck_with_plate(self.company)
        planning = make_planning(self.company, asset=None, assigned_to=self.employee)
        form = ManualTransactionForm(
            data={
                "transaction_type": TransactionTypeChoices.MOVEMENT,
                "asset": asset.pk,
                "cubaEstado": CubaFillState.EMPTY,
                "m3": "6",
                "matricula": truck.pk,
            },
            planning=planning,
        )
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["asset"], asset)
        self.assertEqual(form.cleaned_data["matricula"], "0709CBB")

    def test_valid_without_asset_when_planning_already_has_one(self):
        """The field is optional once the planning already carries its own --
        no need to re-pick what's already there."""
        asset = make_asset(self.company, self.asset_type)
        truck = truck_with_plate(self.company)
        planning = make_planning(self.company, asset=asset, assigned_to=self.employee)
        form = ManualTransactionForm(
            data={
                "transaction_type": TransactionTypeChoices.MOVEMENT,
                "cubaEstado": CubaFillState.EMPTY,
                "m3": "6",
                "matricula": truck.pk,
            },
            planning=planning,
        )
        self.assertTrue(form.is_valid(), form.errors)
        self.assertNotIn("asset", form.fields)

    def test_porte_with_only_a_truck_still_asks_for_the_asset(self):
        truck = truck_with_plate(self.company)
        cargo = make_asset(self.company, self.asset_type, name="Cuba-001")
        planning = make_planning(
            self.company, asset=None, assigned_to=self.employee, planning_type=PlanningType.PORTE,
            transport_asset=truck,
        )
        form = ManualTransactionForm(planning=planning)
        self.assertIn("asset", form.fields)
        self.assertEqual(form.fields["asset"].label, "Cuba")
        choices = set(form.fields["asset"].queryset.values_list("pk", flat=True))
        self.assertIn(cargo.pk, choices)
        self.assertNotIn(truck.pk, choices)
        self.assertNotIn("matricula", form.fields)
        missing = ManualTransactionForm(
            data={"transaction_type": TransactionTypeChoices.PORTE_MOVEMENT},
            planning=planning,
        )
        self.assertFalse(missing.is_valid())
        self.assertIn("asset", missing.errors)
        picked = ManualTransactionForm(
            data={"transaction_type": TransactionTypeChoices.PORTE_MOVEMENT, "asset": cargo.pk},
            planning=planning,
        )
        self.assertTrue(picked.is_valid(), picked.errors)
        self.assertEqual(picked.cleaned_data["asset"], cargo)

    def test_asks_for_the_cuba_when_the_plan_only_has_a_truck(self):
        truck_type = make_asset_type(self.company, name="Camión")
        truck_type.category = AssetTypeCategory.TRUCK
        truck_type.save(update_fields=["category"])
        truck = make_asset(self.company, truck_type, name="Truck-001")
        cuba = make_asset(self.company, self.asset_type, name="Cuba-001")
        planning = make_planning(
            self.company, asset=None, transport_asset=truck, assigned_to=self.employee,
        )
        form = ManualTransactionForm(planning=planning)
        self.assertIn("asset", form.fields)
        self.assertEqual(form.fields["asset"].label, "Cuba")
        choices = set(form.fields["asset"].queryset.values_list("pk", flat=True))
        self.assertIn(cuba.pk, choices)
        self.assertNotIn(truck.pk, choices)

    def test_hides_asset_when_the_planning_already_has_one(self):
        asset = make_asset(self.company, self.asset_type)
        planning = make_planning(self.company, asset=asset, assigned_to=self.employee)
        form = ManualTransactionForm(planning=planning)
        self.assertNotIn("asset", form.fields)

    def test_queryset_is_scoped_to_the_planning_s_company(self):
        other_company = make_company(name="Other Co")
        other_asset_type = make_asset_type(other_company)
        other_asset = make_asset(other_company, other_asset_type)
        planning = make_planning(self.company, asset=None, assigned_to=self.employee)
        form = ManualTransactionForm(
            data={"transaction_type": TransactionTypeChoices.MOVEMENT, "asset": other_asset.pk},
            planning=planning,
        )
        self.assertFalse(form.is_valid())
        self.assertIn("asset", form.errors)


def _shows(form, name):
    return form.fields[name].widget.attrs.get("data-shows-for", "")


class ManualTransactionFormFieldSetTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = make_company()
        cls.user = make_user()
        cls.employee = make_employee(cls.company, cls.user)
        cls.asset_type = make_asset_type(cls.company)
        cls.zone = make_zone(cls.company)

    def test_movement_and_delivery_do_not_share_the_full_container_schema(self):
        planning = make_planning(self.company, assigned_to=self.employee, destination=self.zone)
        form = ManualTransactionForm(planning=planning)
        self.assertNotIn("trabajo", form.fields)
        self.assertNotIn("peso_vertido", form.fields)
        self.assertNotIn("implemento", form.fields)
        self.assertNotIn("tipo_camion", form.fields)
        self.assertNotIn("zone", form.fields)
        self.assertEqual(_shows(form, "vasAVerter"), TransactionTypeChoices.MOVEMENT)
        self.assertEqual(_shows(form, "vertedero"), TransactionTypeChoices.MOVEMENT)
        self.assertEqual(_shows(form, "cubaEstado"), "movement,delivery")
        self.assertEqual(_shows(form, "tipo_cuba"), "movement,delivery")
        self.assertIn("peso_neto", form.fields)
        self.assertEqual(_shows(form, "peso_neto"), TransactionTypeChoices.RECEIPT)
        self.assertEqual(_shows(form, "receipt_zone"), TransactionTypeChoices.RECEIPT)

    def test_hides_m3_and_plate_when_the_planning_already_has_them(self):
        asset = make_asset(self.company, self.asset_type)
        AssetIdentifier.objects.create(asset=asset, label=AssetIdentifierLabel.PLATE, value="0709CBB")
        planning = make_planning(
            self.company, asset=asset, transport_asset=asset, assigned_to=self.employee,
            cuba_size="7m3", destination=self.zone,
        )
        form = ManualTransactionForm(planning=planning)
        self.assertNotIn("m3", form.fields)
        self.assertNotIn("matricula", form.fields)

    def test_delivery_asks_for_a_zone_only_when_the_planning_has_no_destination(self):
        planning = make_planning(self.company, assigned_to=self.employee)
        form = ManualTransactionForm(planning=planning)
        self.assertEqual(_shows(form, "zone"), TransactionTypeChoices.DELIVERY)

    def test_empty_container_does_not_require_material_or_landfill(self):
        asset = make_asset(self.company, self.asset_type)
        AssetIdentifier.objects.create(asset=asset, label=AssetIdentifierLabel.PLATE, value="0709CBB")
        planning = make_planning(
            self.company, asset=asset, assigned_to=self.employee, cuba_size="7m3",
            transport_asset=asset,
        )
        form = ManualTransactionForm(
            data={"transaction_type": TransactionTypeChoices.MOVEMENT, "cubaEstado": CubaFillState.EMPTY},
            planning=planning,
        )
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["tipo_cuba"], "")
        self.assertIsNone(form.cleaned_data["vertedero"])

    def test_full_container_going_to_dump_requires_material_and_landfill(self):
        planning = make_planning(self.company, assigned_to=self.employee, cuba_size="7m3")
        form = ManualTransactionForm(
            data={
                "transaction_type": TransactionTypeChoices.MOVEMENT,
                "cubaEstado": CubaFillState.FULL,
                "vasAVerter": FormAffirmative.YES,
            },
            planning=planning,
        )
        self.assertFalse(form.is_valid())
        self.assertIn("tipo_cuba", form.errors)
        self.assertIn("vertedero", form.errors)

    def test_receipt_requires_albaran_and_landfill(self):
        landfill = make_zone(self.company, name="Vertedero Norte")
        landfill.zone_type = WorkingZone.ZoneType.VERTEDERO
        landfill.save(update_fields=["zone_type"])
        asset = make_asset(self.company, self.asset_type)
        planning = make_planning(
            self.company, asset=asset, assigned_to=self.employee,
            destination=self.zone,
        )
        form = ManualTransactionForm(
            data={
                "transaction_type": TransactionTypeChoices.RECEIPT,
                "nro_albaran": "01083485",
                "receipt_zone": landfill.pk,
            },
            planning=planning,
        )
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["zone"], landfill)
        self.assertEqual(form.build_data()["peso_neto"], "")

    def test_porte_steps_do_not_ask_for_planning_fields(self):
        planning = make_planning(
            self.company, assigned_to=self.employee, planning_type=PlanningType.PORTE,
            load_description="zahorra",
        )
        form = ManualTransactionForm(planning=planning)
        self.assertNotIn("load_description", form.fields)
        self.assertNotIn("client_id", form.fields)
        self.assertNotIn("machine_id", form.fields)
        self.assertEqual(_shows(form, "goods_type"), TransactionTypeChoices.PORTE_LOAD)
        self.assertNotIn(TransactionTypeChoices.PORTE_LOAD, _shows(form, "matricula"))

    def test_delivery_does_not_ask_dump_questions(self):
        asset = make_asset(self.company, self.asset_type)
        truck = truck_with_plate(self.company)
        planning = make_planning(
            self.company, asset=asset, assigned_to=self.employee, cuba_size="7m3",
            destination=self.zone,
        )
        form = ManualTransactionForm(
            data={
                "transaction_type": TransactionTypeChoices.DELIVERY,
                "cubaEstado": CubaFillState.EMPTY,
                "matricula": truck.pk,
            },
            planning=planning,
        )
        self.assertTrue(form.is_valid(), form.errors)
        self.assertNotIn("vasAVerter", form.build_data())
        self.assertNotIn("vertedero", form.build_data())


class ManualTransactionFormReferenceNumberFieldTests(TestCase):
    """reference_number is a plain top-level field (like performed_by/notes),
    not one of the per-type data_schema_fields -- it applies to every
    transaction type and is stored on the model, not in data."""

    @classmethod
    def setUpTestData(cls):
        cls.company = make_company()
        cls.user = make_user()
        cls.employee = make_employee(cls.company, cls.user)
        cls.asset_type = make_asset_type(cls.company)
        cls.asset = make_asset(cls.company, cls.asset_type)
        cls.truck = truck_with_plate(cls.company)

    def _movement_data(self, **extra):
        data = {
            "transaction_type": TransactionTypeChoices.MOVEMENT,
            "cubaEstado": CubaFillState.EMPTY,
            "m3": "6",
            "matricula": self.truck.pk,
        }
        data.update(extra)
        return data

    def test_optional_and_cleaned(self):
        planning = make_planning(self.company, asset=self.asset, assigned_to=self.employee)
        form = ManualTransactionForm(
            data=self._movement_data(reference_number="REF-1"),
            planning=planning,
        )
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["reference_number"], "REF-1")
        self.assertNotIn("reference_number", form.build_data())

    def test_blank_is_valid(self):
        planning = make_planning(self.company, asset=self.asset, assigned_to=self.employee)
        form = ManualTransactionForm(data=self._movement_data(), planning=planning)
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["reference_number"], "")

    def test_present_for_porte_plannings_too(self):
        porte_asset = make_asset(self.company, self.asset_type, name="Truck")
        planning = make_planning(
            self.company, assigned_to=self.employee, planning_type=PlanningType.PORTE,
            transport_asset=porte_asset,
        )
        form = ManualTransactionForm(planning=planning)
        self.assertIn("reference_number", form.fields)
        self.assertNotIn("data-shows-for", form.fields["reference_number"].widget.attrs)
