"""Work entries (revenue) and cost entries (cost) driven by one WorkEntryType config."""
from decimal import Decimal

from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.utils import timezone
from djmoney.money import Money

from assets.models import AssetTransaction, TransactionTypeChoices
from company.models import Company
from invoices.models import AppliedTax, TaxType
from management.models import (
    CostEntry,
    ProjectFile,
    WorkEntry,
    WorkEntryCategory,
    WorkEntryDirection,
    WorkEntryPricingStep,
    WorkEntryType,
)
from management.services.work_entry import WorkEntryService
from sales.models import Client


class EntryFixtureMixin:
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.company = Company.objects.create(name="Taller Co")
        cls.client = Client.objects.create(name="Acme", company=cls.company)
        cls.project_file = ProjectFile.objects.create(company=cls.company, name="PF1", client=cls.client)
        cls.category = WorkEntryCategory.objects.create(company=cls.company, name="Delivery", slug="delivery")

        cls.revenue_type = cls._make_type("Delivery charge", WorkEntryDirection.REVENUE, "25")
        cls.cost_type = cls._make_type("Delivery cost", WorkEntryDirection.COST, "8")

        cls.vat = TaxType.objects.create(tax_name="IVA", country="ES", percentage_options=[0, 21])
        AppliedTax.objects.create(
            tax_type=cls.vat,
            selected_percentage=Decimal("21.00"),
            content_type=ContentType.objects.get_for_model(WorkEntryType),
            object_id=cls.revenue_type.pk,
        )
        AppliedTax.objects.create(
            tax_type=cls.vat,
            selected_percentage=Decimal("21.00"),
            content_type=ContentType.objects.get_for_model(WorkEntryType),
            object_id=cls.cost_type.pk,
        )

        cls.source = AssetTransaction.objects.create(
            company=cls.company,
            project_file=cls.project_file,
            transaction_type=TransactionTypeChoices.DELIVERY,
            date=timezone.now(),
        )

    @classmethod
    def _make_type(cls, name, direction, price_expression, category=None, project_file=None):
        wtype = WorkEntryType.objects.create(
            company=cls.company,
            category=category or cls.category,
            name=name,
            direction=direction,
            project_file=project_file,
        )
        WorkEntryPricingStep.objects.create(entry_type=wtype, order=0, variable_name="price", expression=price_expression)
        return wtype

    def _refresh_pf(self):
        return ProjectFile.objects.get(pk=self.project_file.pk)


class CreateEntryFromSourceTests(EntryFixtureMixin, TestCase):
    def test_revenue_type_creates_work_entry_with_taxes(self):
        entry = WorkEntryService.create_entry_from_source(
            self.revenue_type, self.source, self.project_file, quantity=Decimal("2")
        )
        self.assertIsInstance(entry, WorkEntry)
        self.assertEqual(entry.total_price, Money("50.00", "EUR"))
        self.assertEqual(entry.applied_taxes.count(), 1)
        self.assertEqual(entry.total_with_tax, Money("60.50", "EUR"))

    def test_cost_type_creates_cost_entry_without_taxes(self):
        entry = WorkEntryService.create_entry_from_source(
            self.cost_type, self.source, self.project_file, quantity=Decimal("3")
        )
        self.assertIsInstance(entry, CostEntry)
        self.assertEqual(entry.total_price, Money("24.00", "EUR"))
        self.assertEqual(entry.source, self.source)
        cost_ct = ContentType.objects.get_for_model(CostEntry)
        self.assertFalse(AppliedTax.objects.filter(content_type=cost_ct, object_id=entry.pk).exists())
        self.assertEqual(WorkEntry.objects.count(), 0)


class PriceRoundingTests(EntryFixtureMixin, TestCase):
    def test_unit_price_from_expression_is_rounded_half_up(self):
        thirds = self._make_type("Thirds", WorkEntryDirection.COST, "10 / 3",
                                 category=WorkEntryCategory.objects.create(company=self.company, name="R1", slug="r1"))
        self.assertEqual(thirds.compute_unit_price({}), Decimal("3.33"))

        half = self._make_type("Half", WorkEntryDirection.COST, "2.005",
                               category=WorkEntryCategory.objects.create(company=self.company, name="R2", slug="r2"))
        self.assertEqual(half.compute_unit_price({}), Decimal("2.01"))

    def test_entry_created_from_float_expression_validates(self):
        wtype = self._make_type("Per kg", WorkEntryDirection.COST, "1234.5 * 0.0375",
                                category=WorkEntryCategory.objects.create(company=self.company, name="R3", slug="r3"))
        entry = WorkEntryService.create_entry_from_source(wtype, self.source, self.project_file, quantity=Decimal("1.5"))
        self.assertEqual(entry.unit_price, Money("46.29", "EUR"))   # 46.29375 → 46.29
        self.assertEqual(entry.total_price, Money("69.44", "EUR"))  # 69.435 → 69.44 (half up)


class TypeDirectionTests(EntryFixtureMixin, TestCase):
    def test_same_category_allows_one_type_per_direction(self):
        # Scoped to a project file so the unique constraint has no NULL column
        # (Postgres treats NULLs as distinct, which is pre-existing behaviour
        # for company-wide types).
        self._make_type("PF charge", WorkEntryDirection.REVENUE, "1", project_file=self.project_file)
        self._make_type("PF cost", WorkEntryDirection.COST, "1", project_file=self.project_file)
        with self.assertRaises(IntegrityError), transaction.atomic():
            WorkEntryType.objects.create(
                company=self.company,
                category=self.category,
                name="Duplicate PF charge",
                direction=WorkEntryDirection.REVENUE,
                project_file=self.project_file,
            )

    def test_entry_model_follows_direction(self):
        self.assertIs(self.revenue_type.entry_model, WorkEntry)
        self.assertIs(self.cost_type.entry_model, CostEntry)


class BillingStatusTests(EntryFixtureMixin, TestCase):
    def test_both_directions_expected_and_counted(self):
        status = WorkEntryService.get_billing_status(self.source)
        self.assertEqual({t.pk for t in status["expected"]}, {self.revenue_type.pk, self.cost_type.pk})
        self.assertFalse(status["is_fully_processed"])

        WorkEntryService.create_entry_from_source(self.revenue_type, self.source, self.project_file)
        status = WorkEntryService.get_billing_status(self.source)
        self.assertEqual([t.pk for t in status["missing"]], [self.cost_type.pk])
        self.assertFalse(status["is_fully_processed"])

        WorkEntryService.create_entry_from_source(self.cost_type, self.source, self.project_file)
        status = WorkEntryService.get_billing_status(self.source)
        self.assertTrue(status["is_fully_processed"])
        self.assertEqual(status["count_existing"], 2)

    def test_project_file_override_is_per_direction(self):
        pf_cost = self._make_type("PF delivery cost", WorkEntryDirection.COST, "5", project_file=self.project_file)
        status = WorkEntryService.get_billing_status(self.source)
        expected_ids = {t.pk for t in status["expected"]}
        self.assertIn(pf_cost.pk, expected_ids)
        self.assertNotIn(self.cost_type.pk, expected_ids)
        self.assertIn(self.revenue_type.pk, expected_ids)


class ProjectFileTotalsTests(EntryFixtureMixin, TestCase):
    def test_cost_entry_updates_total_costs_on_save_and_delete(self):
        entry = WorkEntryService.create_entry_from_source(
            self.cost_type, self.source, self.project_file, quantity=Decimal("2")
        )
        self.assertEqual(self._refresh_pf().total_costs, Money("16.00", "EUR"))
        entry.delete()
        self.assertEqual(self._refresh_pf().total_costs, Money("0.00", "EUR"))

    def test_work_entry_updates_total_amount(self):
        WorkEntryService.create_entry_from_source(self.revenue_type, self.source, self.project_file)
        pf = self._refresh_pf()
        self.assertEqual(pf.total_amount, Money("25.00", "EUR"))
        self.assertEqual(pf.total_costs, Money("0.00", "EUR"))

    def test_estimated_margin(self):
        pf = self.project_file
        pf.total_amount = Money("100.00", "EUR")
        pf.total_purchased = Money("30.00", "EUR")
        pf.total_costs = Money("12.50", "EUR")
        self.assertEqual(pf.estimated_margin, Money("57.50", "EUR"))


class EntryValidationTests(EntryFixtureMixin, TestCase):
    def _cost_entry(self, **overrides):
        data = dict(
            company=self.company,
            project_file=self.project_file,
            entry_type=self.cost_type,
            description="x",
            quantity=Decimal("1"),
            unit_price=Money("1", "EUR"),
            date=timezone.localdate(),
        )
        data.update(overrides)
        return CostEntry(**data)

    def _work_entry(self, **overrides):
        data = dict(
            company=self.company,
            project_file=self.project_file,
            entry_type=self.revenue_type,
            description="x",
            quantity=Decimal("1"),
            unit_price=Money("1", "EUR"),
            date=timezone.localdate(),
        )
        data.update(overrides)
        return WorkEntry(**data)

    def test_rejects_non_positive_quantity(self):
        for build in (self._cost_entry, self._work_entry):
            for qty in (Decimal("0"), Decimal("-1")):
                with self.subTest(build=build.__name__, quantity=qty):
                    with self.assertRaises(ValidationError) as ctx:
                        build(quantity=qty).full_clean()
                    self.assertIn("quantity", ctx.exception.message_dict)

    def test_rejects_negative_unit_price(self):
        for build in (self._cost_entry, self._work_entry):
            with self.subTest(build=build.__name__):
                with self.assertRaises(ValidationError) as ctx:
                    build(unit_price=Money("-1", "EUR")).full_clean()
                self.assertIn("unit_price", ctx.exception.message_dict)

    def test_cost_entry_rejects_revenue_type(self):
        with self.assertRaises(ValidationError) as ctx:
            self._cost_entry(entry_type=self.revenue_type).full_clean()
        self.assertIn("entry_type", ctx.exception.message_dict)

    def test_work_entry_rejects_cost_type(self):
        with self.assertRaises(ValidationError) as ctx:
            self._work_entry(entry_type=self.cost_type).full_clean()
        self.assertIn("entry_type", ctx.exception.message_dict)

    def test_db_constraint_blocks_non_positive_cost_quantity(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            self._cost_entry(quantity=Decimal("0")).save()
