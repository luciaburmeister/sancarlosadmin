"""Tests for the payroll period close/reopen workflow."""

from datetime import date, timedelta
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.test import TestCase

from company.models import Company, Employee
from payroll.models import (
    CompanyPayrollConfig,
    EmployeePayroll,
    PayrollImportPendingRow,
    PayrollPeriod,
)
from payroll.services.period_service import (
    _last_business_day,
    _last_day_of_month,
    close_period,
    compute_payment_dates,
    reopen_period,
)


class PeriodCloseTestBase(TestCase):
    def setUp(self):
        self.company = Company.objects.create(
            name="Test Company", tax_identifier="B12345678"
        )
        self.emp1 = Employee.objects.create(
            company=self.company, first_name="JUAN", last_name="GARCIA",
            hired_at=date(2020, 1, 1),
        )
        self.emp2 = Employee.objects.create(
            company=self.company, first_name="ANA", last_name="PEREZ",
            hired_at=date(2020, 1, 1),
        )
        self.period = PayrollPeriod.objects.create(
            company=self.company, year=2026, month=3,
            source=PayrollPeriod.Source.EXCEL_IMPORT,
        )

    def _add_payroll(self, employee, net, employer_ss, irpf, cost):
        return EmployeePayroll.objects.create(
            period=self.period,
            employee=employee,
            company=self.company,
            net_salary=Decimal(net),
            employer_social_security=Decimal(employer_ss),
            irpf_withholding_amount=Decimal(irpf),
            employer_cost=Decimal(cost),
        )


class CloseTests(PeriodCloseTestBase):
    def test_close_aggregates_totals(self):
        self._add_payroll(self.emp1, "840.00", "300.00", "100.00", "1300.00")
        self._add_payroll(self.emp2, "978.00", "360.00", "150.00", "1560.00")

        close_period(self.period)
        self.period.refresh_from_db()

        self.assertEqual(self.period.status, PayrollPeriod.Status.CLOSED)
        self.assertIsNotNone(self.period.closed_at)
        self.assertEqual(self.period.total_net_salary, Decimal("1818.00"))
        self.assertEqual(self.period.total_employer_ss, Decimal("660.00"))
        self.assertEqual(self.period.total_irpf_withheld, Decimal("250.00"))
        self.assertEqual(self.period.total_employer_cost, Decimal("2860.00"))

    def test_close_sets_payment_dates_default(self):
        self._add_payroll(self.emp1, "840.00", "300.00", "100.00", "1300.00")
        close_period(self.period)
        self.period.refresh_from_db()

        self.assertEqual(
            self.period.salary_payment_date, _last_business_day(2026, 3)
        )
        self.assertEqual(
            self.period.ss_payment_date,
            _last_day_of_month(2026, 3) + timedelta(days=30),
        )

    def test_salary_date_uses_configured_day(self):
        CompanyPayrollConfig.objects.create(
            company=self.company, salary_payment_day=25, ss_payment_offset_days=20
        )
        dates = compute_payment_dates(self.period)
        self.assertEqual(dates["salary_payment_date"], date(2026, 3, 25))
        self.assertEqual(
            dates["ss_payment_date"], _last_day_of_month(2026, 3) + timedelta(days=20)
        )

    def test_default_salary_date_is_a_weekday(self):
        # March 2026 ends on a weekday; the helper must never return a weekend.
        for month in range(1, 13):
            d = _last_business_day(2026, month)
            self.assertLess(d.weekday(), 5)

    def test_cannot_close_with_pending_rows(self):
        self._add_payroll(self.emp1, "840.00", "300.00", "100.00", "1300.00")
        PayrollImportPendingRow.objects.create(
            period=self.period, a3_code="000099", full_name="UNKNOWN PERSON",
        )
        with self.assertRaises(ValidationError):
            close_period(self.period)
        self.period.refresh_from_db()
        self.assertEqual(self.period.status, PayrollPeriod.Status.DRAFT)

    def test_cannot_close_empty_period(self):
        with self.assertRaises(ValidationError):
            close_period(self.period)

    def test_cannot_close_already_closed(self):
        self._add_payroll(self.emp1, "840.00", "300.00", "100.00", "1300.00")
        close_period(self.period)
        with self.assertRaises(ValidationError):
            close_period(self.period)


class ReopenTests(PeriodCloseTestBase):
    def test_reopen_resets_status(self):
        self._add_payroll(self.emp1, "840.00", "300.00", "100.00", "1300.00")
        close_period(self.period)

        reopen_period(self.period)
        self.period.refresh_from_db()
        self.assertEqual(self.period.status, PayrollPeriod.Status.DRAFT)
        self.assertIsNone(self.period.closed_at)

    def test_cannot_reopen_draft(self):
        with self.assertRaises(ValidationError):
            reopen_period(self.period)
