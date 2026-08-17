"""
assets/tests/test_compatibility.py

TransportCompatibilityRule + assets.services.compatibility: business users
write conditions like `truck.carga_maxima_kg >= cuba.peso_kg` in the admin,
and every active rule for the company must pass for a truck to be usable.

Run just this file:
    python manage.py test assets.tests.test_compatibility
"""
from datetime import date

from django.core.exceptions import ValidationError
from django.test import TestCase

from assets.models import AssetPlanning, TransportCompatibilityRule
from assets.services.compatibility import CompatibilityChecker
from api.fleet.tests.helpers import make_asset, make_asset_type, make_company, make_zone


class TransportCompatibilityRuleModelTests(TestCase):
    def setUp(self):
        self.company = make_company()

    def test_rejects_empty_condition(self):
        rule = TransportCompatibilityRule(company=self.company, name="Empty", condition_expression="   ")
        with self.assertRaises(ValidationError):
            rule.full_clean()

    def test_rejects_unsafe_expression(self):
        rule = TransportCompatibilityRule(
            company=self.company, name="Unsafe", condition_expression="__import__('os')"
        )
        with self.assertRaises(ValidationError):
            rule.full_clean()

    def test_accepts_valid_expression(self):
        rule = TransportCompatibilityRule(
            company=self.company, name="Peso", condition_expression="truck.carga_maxima_kg >= cuba.peso_kg",
        )
        rule.full_clean()  # no exception


class CompatibilityCheckerTests(TestCase):
    def setUp(self):
        self.company = make_company()
        self.zone = make_zone(self.company)
        self.cuba_type = make_asset_type(self.company, "Cuba 6m3")
        self.truck_type = make_asset_type(self.company, "Camion")

        self.cuba = make_asset(self.company, self.cuba_type, zone=self.zone, name="CUBA-001")
        self.cuba.data = {"peso_kg": 1000, "tipo_gancho": "gancho_a"}
        self.cuba.save(update_fields=["data"])

        self.planning = AssetPlanning.objects.create(
            company=self.company,
            asset=self.cuba,
            origin=self.zone,
            destination=self.zone,
            scheduled_date=date(2026, 1, 1),
            cuba_size="7m3",
            cuba_operation_type="puesta",
        )

    def _truck(self, **data):
        truck = make_asset(self.company, self.truck_type, name="TRUCK-001")
        truck.data = data
        truck.save(update_fields=["data"])
        return truck

    def _checker(self):
        # Built after rules/exclusions are set up in each test — it caches
        # the active rules at construction time (see CompatibilityChecker).
        return CompatibilityChecker(self.planning)

    def test_no_active_rules_means_everything_is_compatible(self):
        truck = self._truck()
        checker = self._checker()
        self.assertTrue(checker.is_compatible(truck))
        self.assertEqual(checker.reasons(truck), [])

    def test_truck_passing_all_active_rules_is_compatible(self):
        TransportCompatibilityRule.objects.create(
            company=self.company, name="Peso", condition_expression="truck.carga_maxima_kg >= cuba.peso_kg",
        )
        TransportCompatibilityRule.objects.create(
            company=self.company, name="Enganche", condition_expression="truck.gancho == cuba.tipo_gancho",
        )
        truck = self._truck(carga_maxima_kg=2000, gancho="gancho_a")
        self.assertTrue(self._checker().is_compatible(truck))

    def test_truck_failing_one_of_several_rules_is_not_compatible(self):
        TransportCompatibilityRule.objects.create(
            company=self.company, name="Peso", condition_expression="truck.carga_maxima_kg >= cuba.peso_kg",
        )
        TransportCompatibilityRule.objects.create(
            company=self.company, name="Enganche", condition_expression="truck.gancho == cuba.tipo_gancho",
        )
        # Enough weight capacity, wrong hook — must fail overall (AND, not OR).
        truck = self._truck(carga_maxima_kg=2000, gancho="gancho_b")
        checker = self._checker()
        self.assertFalse(checker.is_compatible(truck))
        self.assertEqual(checker.reasons(truck), ["Enganche"])

    def test_inactive_rules_are_ignored(self):
        TransportCompatibilityRule.objects.create(
            company=self.company, name="Peso", is_active=False,
            condition_expression="truck.carga_maxima_kg >= cuba.peso_kg",
        )
        truck = self._truck()  # no carga_maxima_kg at all
        self.assertTrue(self._checker().is_compatible(truck))

    def test_rules_from_other_companies_are_ignored(self):
        other_company = make_company("Other Co")
        TransportCompatibilityRule.objects.create(
            company=other_company, name="Peso", condition_expression="truck.carga_maxima_kg >= cuba.peso_kg",
        )
        truck = self._truck()  # no carga_maxima_kg — would fail if the rule applied
        self.assertTrue(self._checker().is_compatible(truck))

    def test_missing_data_field_fails_closed_instead_of_raising(self):
        TransportCompatibilityRule.objects.create(
            company=self.company, name="Peso", condition_expression="truck.carga_maxima_kg >= cuba.peso_kg",
        )
        truck = self._truck()  # no carga_maxima_kg in data at all
        checker = self._checker()
        self.assertFalse(checker.is_compatible(truck))
        self.assertEqual(checker.reasons(truck), ["Peso"])

    def test_context_exposes_cuba_size_and_operation_type_from_the_planning(self):
        truck = self._truck()
        context = self._checker().context_for(truck)
        self.assertEqual(context["cuba"]["size"], "7m3")
        self.assertEqual(context["cuba"]["operation_type"], "puesta")
        self.assertEqual(context["cuba"]["peso_kg"], 1000)

    def test_context_handles_a_planning_without_an_asset(self):
        self.planning.asset = None
        self.planning.save(update_fields=["asset"])
        truck = self._truck()
        context = self._checker().context_for(truck)
        # No underlying asset -> no asset.data fields, but cuba_size/operation_type still show up.
        self.assertEqual(context["cuba"]["size"], "7m3")

    def test_no_asset_means_compatibility_is_a_no_op_not_fail_closed(self):
        # A porte with no asset (e.g. porte_material) shouldn't be blocked by
        # rules that were written in terms of a cuba that doesn't exist here.
        TransportCompatibilityRule.objects.create(
            company=self.company, name="Peso", condition_expression="truck.carga_maxima_kg >= cuba.peso_kg",
        )
        self.planning.asset = None
        self.planning.save(update_fields=["asset"])
        truck = self._truck()  # no carga_maxima_kg — would fail closed if the rule still ran
        checker = self._checker()
        self.assertTrue(checker.is_compatible(truck))
        self.assertEqual(checker.reasons(truck), [])

    def test_directly_excluded_asset_is_not_compatible_even_if_it_passes_every_rule(self):
        TransportCompatibilityRule.objects.create(
            company=self.company, name="Peso", condition_expression="truck.carga_maxima_kg >= cuba.peso_kg",
        )
        truck = self._truck(carga_maxima_kg=2000)  # passes the rule on paper
        self.cuba.excluded_assets.add(truck)  # but this specific pair is manually blocked
        checker = self._checker()
        self.assertFalse(checker.is_compatible(truck))
        self.assertIn("Manually excluded", checker.reasons(truck))

    def test_exclusion_blocks_even_with_no_rules_configured_at_all(self):
        truck = self._truck()
        self.cuba.excluded_assets.add(truck)
        self.assertFalse(self._checker().is_compatible(truck))

    def test_exclusion_is_mutual_regardless_of_which_side_declares_it(self):
        truck = self._truck()
        # Declared from the truck's side, not the cuba's.
        truck.excluded_assets.add(self.cuba)
        self.assertFalse(self._checker().is_compatible(truck))

    def test_non_excluded_truck_is_unaffected_by_another_pair_s_exclusion(self):
        excluded_truck = self._truck()
        other_truck = self._truck()
        self.cuba.excluded_assets.add(excluded_truck)
        self.assertTrue(self._checker().is_compatible(other_truck))

    def test_checker_caches_rules_so_a_later_rule_change_does_not_affect_it(self):
        checker = self._checker()  # no rules exist yet
        TransportCompatibilityRule.objects.create(
            company=self.company, name="Peso", condition_expression="truck.carga_maxima_kg >= cuba.peso_kg",
        )
        truck = self._truck()  # would fail "Peso" if the checker saw it
        self.assertTrue(checker.is_compatible(truck))
        # A fresh checker built after the rule exists does see it.
        self.assertFalse(self._checker().is_compatible(truck))
