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
from assets.models import PlanningType, TransactionTypeChoices


class ManualTransactionFormAssetFieldTests(TestCase):
    def setUp(self):
        self.company = make_company()
        self.user = make_user()
        self.employee = make_employee(self.company, self.user)
        self.asset_type = make_asset_type(self.company)
        self.zone = make_zone(self.company)

    def test_invalid_without_asset_when_planning_has_none(self):
        planning = make_planning(self.company, asset=None, assigned_to=self.employee)
        form = ManualTransactionForm(
            data={"transaction_type": TransactionTypeChoices.MOVEMENT},
            planning=planning,
        )
        self.assertFalse(form.is_valid())
        self.assertIn("asset", form.errors)

    def test_valid_when_an_asset_is_picked_for_an_assetless_planning(self):
        asset = make_asset(self.company, self.asset_type, zone=self.zone)
        planning = make_planning(self.company, asset=None, assigned_to=self.employee)
        form = ManualTransactionForm(
            data={"transaction_type": TransactionTypeChoices.MOVEMENT, "asset": asset.pk},
            planning=planning,
        )
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["asset"], asset)

    def test_valid_without_asset_when_planning_already_has_one(self):
        """The field is optional once the planning already carries its own --
        no need to re-pick what's already there."""
        asset = make_asset(self.company, self.asset_type, zone=self.zone)
        planning = make_planning(self.company, asset=asset, assigned_to=self.employee)
        form = ManualTransactionForm(
            data={"transaction_type": TransactionTypeChoices.MOVEMENT},
            planning=planning,
        )
        self.assertTrue(form.is_valid(), form.errors)
        self.assertIsNone(form.cleaned_data.get("asset"))

    def test_valid_without_asset_when_planning_has_a_transport_asset(self):
        truck = make_asset(self.company, self.asset_type, name="Truck-001")
        planning = make_planning(
            self.company, assigned_to=self.employee, planning_type=PlanningType.PORTE,
            transport_asset=truck,
        )
        form = ManualTransactionForm(
            data={"transaction_type": TransactionTypeChoices.PORTE_MOVEMENT},
            planning=planning,
        )
        self.assertTrue(form.is_valid(), form.errors)

    def test_pre_fills_from_the_planning_s_existing_asset(self):
        asset = make_asset(self.company, self.asset_type, zone=self.zone)
        planning = make_planning(self.company, asset=asset, assigned_to=self.employee)
        form = ManualTransactionForm(planning=planning)
        self.assertEqual(form.fields["asset"].initial, asset.pk)

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
