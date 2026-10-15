"""Tests for the tabular A3 payroll layout (employees as rows, DNI matching)."""

from datetime import date
from decimal import Decimal
from io import BytesIO

import openpyxl
from django.test import TestCase

from payroll.models import (
    EmployeePayroll,
    PayrollImportPendingRow,
    PayrollSSBreakdown,
)
from payroll.services.a3_importer import (
    A3PayrollImportService,
    resolve_pending_row,
)
from payroll.tests.factories import make_company, make_employee, make_importer_employees

# Column headers, in order. Concept columns carry a leading A3 code; the
# rest are totals/summary columns identified by their (normalized) label.
TABULAR_HEADERS = [
    "Trabajador",        # 0
    "DNI",               # 1
    "Apellidos y Nombre",  # 2
    "0001 Sal.Base",     # 3  accrual concept
    "TOT. BRUTO",        # 4
    "0995-COTIZ.CC",     # 5  deduction concept (SS common)
    "0999-TRI.IRPF",     # 6  deduction concept (IRPF)
    "TOT.DEDUC.",        # 7
    "TOTAL.LIQ.",        # 8
    "SS EMPRESA",        # 9
    "SS TRAB.",          # 10
    "COSTE EMP.",        # 11
    "BASE CC",           # 12
    "B.IRPF DIN",        # 13
    "DIAS COT.",         # 14
]

MATCHED = {
    "dni": "12345678Z", "name": "GARCIA LOPEZ, JUAN",
    "base": 1000, "bruto": 1000, "ss995": 60, "irpf": 100, "deduc": 160,
    "liquido": 840, "ss_emp": 300, "ss_trab": 60, "coste": 1300,
    "base_cc": 1000, "base_irpf": 1000, "dias": 30,
}
SECOND = {
    "dni": "87654321X", "name": "PEREZ SANZ, ANA",
    "base": 1200, "bruto": 1200, "ss995": 72, "irpf": 150, "deduc": 222,
    "liquido": 978, "ss_emp": 360, "ss_trab": 72, "coste": 1560,
    "base_cc": 1200, "base_irpf": 1200, "dias": 30,
}


def build_tabular_workbook(*, nif="B12345678", employees=None):
    """Build an in-memory tabular A3 workbook (one row per employee)."""
    if employees is None:
        employees = [dict(MATCHED), dict(SECOND)]

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "1"

    ws["A1"] = "Moneda: Euro"
    ws["A2"] = "DEL 01/03/26 AL 31/03/26"
    ws["A3"] = f"Empresa:    15-TEST COMPANY SL NIF: {nif}"

    header_row = 5
    for idx, label in enumerate(TABULAR_HEADERS):
        ws.cell(row=header_row, column=idx + 1, value=label)

    for r, emp in enumerate(employees, start=header_row + 1):
        values = [
            "",            # Trabajador
            emp["dni"],    # DNI
            emp["name"],   # Apellidos y Nombre
            emp["base"], emp["bruto"], emp["ss995"], emp["irpf"], emp["deduc"],
            emp["liquido"], emp["ss_emp"], emp["ss_trab"], emp["coste"],
            emp["base_cc"], emp["base_irpf"], emp["dias"],
        ]
        for idx, value in enumerate(values):
            ws.cell(row=r, column=idx + 1, value=value)

    buffer = BytesIO()
    wb.save(buffer)
    buffer.seek(0)
    return buffer


class TabularImporterTestBase(TestCase):
    def setUp(self):
        self.company = make_company()
        self.emp1, self.emp2 = make_importer_employees(
            company=self.company, dnis=("12345678Z", "87654321X"),
        )


class TabularValidateTests(TabularImporterTestBase):
    def test_layout_is_detected_as_tabular_and_matched_by_dni(self):
        report = A3PayrollImportService(build_tabular_workbook()).validate()
        self.assertEqual(report.layout, "tabular")
        self.assertFalse(report.has_blocking_errors, report.errors)
        self.assertEqual(report.matched_count, 2)
        self.assertEqual(report.unmatched_count, 0)
        row = next(r for r in report.rows if r.dni == "12345678Z")
        self.assertEqual(row.employee_id, self.emp1.id)

    def test_dni_match_ignores_name_mismatch(self):
        # DNI matches emp1 even though the name in the file is unrelated.
        emps = [dict(MATCHED, name="NOMBRE TOTALMENTE, DISTINTO")]
        report = A3PayrollImportService(build_tabular_workbook(employees=emps)).validate()
        row = report.rows[0]
        self.assertEqual(row.status, "ok")
        self.assertEqual(row.employee_id, self.emp1.id)

    def test_unknown_dni_is_unmatched(self):
        emps = [dict(MATCHED, dni="99999999R")]
        report = A3PayrollImportService(build_tabular_workbook(employees=emps)).validate()
        self.assertEqual(report.rows[0].status, "unmatched")

    def test_duplicate_dni_is_blocking(self):
        emps = [dict(MATCHED), dict(SECOND, dni="12345678Z")]
        report = A3PayrollImportService(build_tabular_workbook(employees=emps)).validate()
        self.assertTrue(report.has_blocking_errors)


class TabularCommitTests(TabularImporterTestBase):
    def test_commit_creates_payrolls_with_values(self):
        period = A3PayrollImportService(
            build_tabular_workbook(), source_file="march.xls"
        ).commit()
        self.assertEqual(period.payrolls.count(), 2)

        p1 = EmployeePayroll.objects.get(period=period, employee=self.emp1)
        self.assertEqual(p1.net_salary, Decimal("840.00"))
        self.assertEqual(p1.gross_salary, Decimal("1000.00"))
        self.assertEqual(p1.employer_cost, Decimal("1300.00"))
        self.assertEqual(p1.employee_social_security, Decimal("60.00"))
        self.assertEqual(p1.employer_social_security, Decimal("300.00"))
        self.assertEqual(p1.irpf_withholding_amount, Decimal("100.00"))
        self.assertEqual(p1.days_in_period, 30)
        # concept lines: salario base, COTIZ.CC (995), TRI.IRPF (999)
        self.assertEqual(p1.concept_lines.count(), 3)
        self.assertTrue(
            p1.ss_breakdown.filter(
                concept=PayrollSSBreakdown.SSConcept.COMMON_CONTINGENCIES
            ).exists()
        )
        # Concept codes are normalized (no leading zeros) -> shared with pivoted.
        self.assertTrue(p1.concept_lines.filter(concept__code="1").exists())
        self.assertTrue(p1.concept_lines.filter(concept__code="995").exists())

    def test_reimport_is_idempotent(self):
        A3PayrollImportService(build_tabular_workbook()).commit()
        period = A3PayrollImportService(build_tabular_workbook()).commit()
        self.assertEqual(period.payrolls.count(), 2)
        p1 = EmployeePayroll.objects.get(period=period, employee=self.emp1)
        self.assertEqual(p1.concept_lines.count(), 3)


class TabularPendingTests(TabularImporterTestBase):
    def test_unmatched_dni_becomes_pending_with_dni(self):
        emps = [dict(MATCHED), dict(SECOND, dni="99999999R")]
        period = A3PayrollImportService(
            build_tabular_workbook(employees=emps), source_file="march.xls"
        ).commit()
        self.assertEqual(period.payrolls.count(), 1)
        pending = period.pending_rows.get()
        self.assertEqual(pending.dni, "99999999R")
        self.assertEqual(pending.a3_code, "")
        self.assertEqual(pending.reason, PayrollImportPendingRow.Reason.UNMATCHED)

    def test_resolving_backfills_the_dni_and_matches_on_reimport(self):
        emps = [dict(MATCHED, dni="99999999R", name="NUEVO EMPLEADO, PEPE")]
        period = A3PayrollImportService(build_tabular_workbook(employees=emps)).commit()
        pending = period.pending_rows.get()
        new_emp = make_employee(
            company=self.company, first_name="PEPE", last_name="NUEVO EMPLEADO",
            hired_at=date(2021, 1, 1),
        )
        resolve_pending_row(pending, new_emp)

        new_emp.refresh_from_db()
        self.assertEqual(new_emp.identification_number, "99999999R")
        self.assertEqual(period.pending_rows.count(), 0)
        self.assertEqual(period.payrolls.count(), 1)

        period = A3PayrollImportService(build_tabular_workbook(employees=emps)).commit()
        self.assertEqual(period.pending_rows.count(), 0)
        self.assertEqual(period.payrolls.count(), 1)
