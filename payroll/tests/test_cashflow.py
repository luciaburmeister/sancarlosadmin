"""Tests for the payroll cash-flow projection service."""

from datetime import date
from decimal import Decimal

from django.test import TestCase

from company.models import Company
from payroll.models import PayrollPeriod
from payroll.services.cashflow import build_week_ranges, get_payroll_outflows


class CashflowTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(
            name="Test Company", tax_identifier="B12345678"
        )
        self.date_from = date(2026, 4, 1)
        self.date_to = date(2026, 5, 31)

    def _make_period(self, *, month, status, salary_date, ss_date,
                     net="1818.00", employer_ss="660.00", employee_ss="132.00",
                     irpf="250.00"):
        return PayrollPeriod.objects.create(
            company=self.company,
            year=2026,
            month=month,
            status=status,
            total_net_salary=Decimal(net),
            total_employer_ss=Decimal(employer_ss),
            total_employee_ss=Decimal(employee_ss),
            total_irpf_withheld=Decimal(irpf),
            salary_payment_date=salary_date,
            ss_payment_date=ss_date,
        )

    def test_closed_period_is_bucketed(self):
        self._make_period(
            month=4, status=PayrollPeriod.Status.CLOSED,
            salary_date=date(2026, 4, 30), ss_date=date(2026, 5, 30),
        )
        result = get_payroll_outflows(
            [self.company.id], self.date_from, self.date_to
        )

        self.assertEqual(result["net_total"], 1818.0)
        self.assertEqual(result["ss_total"], 792.0)  # 660 + 132
        self.assertEqual(result["total"], 2610.0)
        self.assertEqual(result["irpf_total"], 250.0)

        week_sum = sum(w["amount"] for w in result["weeks"])
        self.assertAlmostEqual(week_sum, 2610.0, places=2)
        # Exactly one week carries the salary and one carries the SS
        self.assertEqual(sum(1 for w in result["weeks"] if w["salary"] > 0), 1)
        self.assertEqual(sum(1 for w in result["weeks"] if w["ss"] > 0), 1)

    def test_salary_lands_in_correct_week(self):
        self._make_period(
            month=4, status=PayrollPeriod.Status.CLOSED,
            salary_date=date(2026, 4, 30), ss_date=date(2026, 5, 30),
        )
        weeks = build_week_ranges(self.date_from, self.date_to)
        salary_idx = next(
            i for i, w in enumerate(weeks)
            if w["start"] <= date(2026, 4, 30) <= w["end"]
        )
        result = get_payroll_outflows(
            [self.company.id], self.date_from, self.date_to, weeks
        )
        self.assertEqual(result["weeks"][salary_idx]["salary"], 1818.0)
        self.assertEqual(result["weeks"][salary_idx]["ss"], 0.0)

    def test_draft_period_excluded(self):
        self._make_period(
            month=4, status=PayrollPeriod.Status.DRAFT,
            salary_date=date(2026, 4, 30), ss_date=date(2026, 5, 30),
        )
        result = get_payroll_outflows(
            [self.company.id], self.date_from, self.date_to
        )
        self.assertEqual(result["total"], 0.0)

    def test_period_outside_range_excluded(self):
        self._make_period(
            month=2, status=PayrollPeriod.Status.CLOSED,
            salary_date=date(2026, 2, 27), ss_date=date(2026, 3, 30),
        )
        result = get_payroll_outflows(
            [self.company.id], self.date_from, self.date_to
        )
        self.assertEqual(result["total"], 0.0)

    def test_empty_company_list_returns_zero(self):
        self._make_period(
            month=4, status=PayrollPeriod.Status.CLOSED,
            salary_date=date(2026, 4, 30), ss_date=date(2026, 5, 30),
        )
        result = get_payroll_outflows([], self.date_from, self.date_to)
        self.assertEqual(result["total"], 0.0)
        self.assertTrue(all(w["amount"] == 0.0 for w in result["weeks"]))

    def test_ss_only_when_date_in_range(self):
        # Salary paid before the range, SS settlement inside it: only SS counts.
        self._make_period(
            month=3, status=PayrollPeriod.Status.CLOSED,
            salary_date=date(2026, 3, 31), ss_date=date(2026, 4, 30),
        )
        result = get_payroll_outflows(
            [self.company.id], self.date_from, self.date_to
        )
        self.assertEqual(result["net_total"], 0.0)
        self.assertEqual(result["ss_total"], 792.0)
        self.assertEqual(result["total"], 792.0)
