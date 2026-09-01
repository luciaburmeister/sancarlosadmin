"""
Cost center tree: hierarchy helpers and the validation that keeps the tree sane.

Run just this file:
    python manage.py test accounting.tests_cost_center
"""

from django.core.exceptions import ValidationError
from django.test import TestCase

from accounting.models import CostCenter
from company.models import Company


class CostCenterTreeTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name="Test Company", tax_identifier="B12345678")
        cls.other_company = Company.objects.create(name="Otra SA", tax_identifier="B87654321")

        cls.operaciones = CostCenter.objects.create(
            company=cls.company, code="200", name="Operaciones", is_assignable=False
        )
        cls.flota = CostCenter.objects.create(
            company=cls.company, code="210", name="Flota",
            parent=cls.operaciones, is_assignable=False,
        )
        cls.cuba = CostCenter.objects.create(
            company=cls.company, code="211", name="Flota Cuba", parent=cls.flota
        )
        cls.portes = CostCenter.objects.create(
            company=cls.company, code="212", name="Flota Portes", parent=cls.flota
        )
        cls.taller = CostCenter.objects.create(
            company=cls.company, code="220", name="Taller", parent=cls.operaciones
        )

    # ---- Hierarchy helpers ----

    def test_ancestors_are_root_first_and_exclude_self(self):
        self.assertEqual(
            [node.pk for node in self.cuba.ancestors()],
            [self.operaciones.pk, self.flota.pk],
        )

    def test_root_has_no_ancestors(self):
        self.assertEqual(self.operaciones.ancestors(), [])

    def test_full_name_joins_the_path(self):
        self.assertEqual(self.cuba.full_name, "Operaciones / Flota / Flota Cuba")
        self.assertEqual(self.operaciones.full_name, "Operaciones")

    def test_descendant_ids_covers_the_whole_subtree(self):
        self.assertEqual(
            self.operaciones.descendant_ids(),
            {self.operaciones.pk, self.flota.pk, self.cuba.pk, self.portes.pk, self.taller.pk},
        )
        self.assertEqual(
            self.flota.descendant_ids(include_self=False),
            {self.cuba.pk, self.portes.pk},
        )

    def test_descendant_ids_of_a_leaf_is_just_itself(self):
        self.assertEqual(self.cuba.descendant_ids(), {self.cuba.pk})
        self.assertEqual(self.cuba.descendant_ids(include_self=False), set())

    def test_str_shows_code_and_name(self):
        self.assertEqual(str(self.cuba), "211 Flota Cuba")

    # ---- Validation ----

    def test_cannot_be_its_own_parent(self):
        self.cuba.parent = self.cuba
        with self.assertRaises(ValidationError) as ctx:
            self.cuba.clean()
        self.assertIn("parent", ctx.exception.message_dict)

    def test_cannot_create_a_cycle(self):
        # Making the root a child of its own grandchild would close the loop.
        self.operaciones.parent = self.cuba
        with self.assertRaises(ValidationError) as ctx:
            self.operaciones.clean()
        self.assertIn("parent", ctx.exception.message_dict)

    def test_parent_must_be_in_the_same_company(self):
        foreign = CostCenter.objects.create(
            company=self.other_company, code="200", name="Operaciones"
        )
        node = CostCenter(company=self.company, code="300", name="Estructura", parent=foreign)
        with self.assertRaises(ValidationError) as ctx:
            node.clean()
        self.assertIn("parent", ctx.exception.message_dict)

    def test_valid_reparent_passes(self):
        self.taller.parent = self.flota
        self.taller.clean()  # must not raise

    def test_code_is_unique_per_company_only(self):
        # Same code in another company is fine — it is scoped, not global.
        CostCenter.objects.create(company=self.other_company, code="211", name="Flota Cuba")
        with self.assertRaises(Exception):
            CostCenter.objects.create(company=self.company, code="211", name="Duplicado")
