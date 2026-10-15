"""Tests for the A3 payroll Excel importer."""

from datetime import date
from decimal import Decimal
from io import BytesIO

import openpyxl
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.utils import translation

from payroll.models import (
    EmployeePayroll,
    EmployeePayrollProfile,
    PayrollConcept,
    PayrollImportPendingRow,
    PayrollPeriod,
    PayrollSSBreakdown,
)
from payroll.services.a3_importer import (
    A3PayrollImportService,
    A3PayrollParseError,
    resolve_pending_row,
)
from payroll.tests.factories import make_company, make_employee, make_importer_employees

MATCHED_EMP = {
    "code": "000001", "s1": "GARCIA", "s2": "LOPEZ", "given": "JUAN",
    "base": 1000, "ss995": 60, "irpf": 100, "bruto": 1000,
    "deducciones": 160, "liquido": 840, "ss_emp": 300, "ss_trab": 60,
    "coste": 1300, "base_cc": 1000, "base_at": 1000, "base_irpf": 1000, "dias": 30,
}
UNMATCHED_EMP = {
    "code": "000003", "s1": "NUEVO", "s2": "EMPLEADO", "given": "PEPE",
    "base": 500, "ss995": 30, "irpf": 50, "bruto": 500,
    "deducciones": 80, "liquido": 420, "ss_emp": 150, "ss_trab": 30,
    "coste": 650, "base_cc": 500, "base_at": 500, "base_irpf": 500, "dias": 30,
}


def build_a3_workbook(
    *,
    nif="B12345678",
    employees=None,
    duplicate_code=False,
):
    """Build an in-memory A3-style workbook with the pivoted layout."""
    if employees is None:
        employees = [
            {"code": "000001", "s1": "GARCIA", "s2": "LOPEZ", "given": "JUAN",
             "base": 1000, "ss995": 60, "irpf": 100, "bruto": 1000,
             "deducciones": 160, "liquido": 840, "ss_emp": 300, "ss_trab": 60,
             "coste": 1300, "base_cc": 1000, "base_at": 1000, "base_irpf": 1000, "dias": 30},
            {"code": "000002", "s1": "PEREZ", "s2": "SANZ", "given": "ANA",
             "base": 1200, "ss995": 72, "irpf": 150, "bruto": 1200,
             "deducciones": 222, "liquido": 978, "ss_emp": 360, "ss_trab": 72,
             "coste": 1560, "base_cc": 1200, "base_at": 1200, "base_irpf": 1200, "dias": 30},
        ]
    if duplicate_code:
        employees[1]["code"] = employees[0]["code"]

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "1"

    # Header
    ws["A2"] = "Moneda: Euro"
    ws["A3"] = "PAGA        TOTAL"
    ws["A4"] = "DEL 01/03/26 AL 31/03/26"
    ws["A5"] = f"Empresa:    15-TEST COMPANY SL NIF: {nif}"
    ws["A9"] = "CONCEPTO"

    # Employee columns start at D (col 4)
    for idx, emp in enumerate(employees):
        col = 4 + idx
        ws.cell(row=4, column=col, value=emp["code"])
        ws.cell(row=5, column=col, value=emp["s1"])
        ws.cell(row=6, column=col, value=emp["s2"])
        ws.cell(row=7, column=col, value=emp["given"])

    total_col = 4 + len(employees)  # trailing TOTAL column (no employee code)

    def write_row(r, label, code, name, key):
        if label:
            ws.cell(row=r, column=1, value=label)
        if code is not None:
            ws.cell(row=r, column=2, value=code)
            ws.cell(row=r, column=3, value=name)
        for idx, emp in enumerate(employees):
            ws.cell(row=r, column=4 + idx, value=emp[key])
        ws.cell(row=r, column=total_col, value=sum(e[key] for e in employees))

    write_row(11, None, 1, "Salario Base", "base")
    write_row(27, "TOTAL BRUTO", None, None, "bruto")
    write_row(31, None, 995, "COTIZACION CONT.COMUNES", "ss995")
    write_row(34, None, 999, "TRIBUTACION I.R.P.F.", "irpf")
    write_row(36, "TOTAL DEDUCCIONES", None, None, "deducciones")
    write_row(38, "TOTAL LIQUIDO", None, None, "liquido")
    write_row(40, "SEGURIDAD SOCIAL EMPRESA", None, None, "ss_emp")
    write_row(41, "SEGURIDAD SOCIAL TRABAJADOR", None, None, "ss_trab")
    write_row(45, "COSTE EMPRESA", None, None, "coste")
    write_row(46, "BASE CONTINGENCIAS COMUNES", None, None, "base_cc")
    write_row(47, "BASE ACCIDENTES", None, None, "base_at")
    write_row(48, "BASE I.R.P.F. DINERARIA", None, None, "base_irpf")
    write_row(56, "DIAS COTIZADOS", None, None, "dias")

    # Sheet 2 — company totals
    ws2 = wb.create_sheet("2")
    ws2["A4"] = "DEL 01/03/26 AL 31/03/26"
    ws2["D4"] = "TOTAL"
    total_liquido = sum(e["liquido"] for e in employees)
    ws2["A38"] = "TOTAL LIQUIDO"
    ws2["D38"] = total_liquido
    ws2["A57"] = "TOTAL TRABAJADORES"
    ws2["D57"] = len(employees)

    buffer = BytesIO()
    wb.save(buffer)
    buffer.seek(0)
    return buffer


class A3ImporterTestBase(TestCase):
    def setUp(self):
        self.company = make_company()
        self.emp1, self.emp2 = make_importer_employees(company=self.company)


class ValidateTests(A3ImporterTestBase):
    def test_happy_path_matches_company_and_employees(self):
        report = A3PayrollImportService(build_a3_workbook()).validate()
        self.assertFalse(report.has_blocking_errors, report.errors)
        self.assertEqual(report.company_id, self.company.id)
        self.assertEqual(report.year, 2026)
        self.assertEqual(report.month, 3)
        self.assertEqual(report.matched_count, 2)
        self.assertEqual(report.unmatched_count, 0)
        self.assertEqual(report.total_liquido, Decimal("1818.00"))

    def test_nif_not_found_asks_for_company(self):
        report = A3PayrollImportService(build_a3_workbook(nif="X99999999")).validate()
        self.assertFalse(report.has_blocking_errors)
        self.assertTrue(report.needs_company_selection)
        self.assertEqual(report.company_candidate_ids, [])
        self.assertIsNone(report.company_id)
        self.assertFalse(report.can_commit)

    def test_duplicate_nif_asks_for_company_with_candidates(self):
        twin = make_company(name="Twin Company")
        report = A3PayrollImportService(build_a3_workbook()).validate()
        self.assertFalse(report.has_blocking_errors)
        self.assertTrue(report.needs_company_selection)
        self.assertCountEqual(report.company_candidate_ids, [self.company.id, twin.id])
        self.assertFalse(report.can_commit)

    def test_rows_stay_pending_until_company_resolved(self):
        """Without a resolved company, matching never runs: rows must not read 'ok'."""
        make_company(name="Twin Company")
        report = A3PayrollImportService(build_a3_workbook()).validate()
        self.assertTrue(report.needs_company_selection)
        self.assertTrue(report.rows)
        for row in report.rows:
            self.assertEqual(row.status, "pending")
            self.assertIsNone(row.employee_id)
            self.assertFalse(row.is_importable)
        self.assertEqual(report.matched_count, 0)

    def test_manual_company_resolves_duplicate_nif(self):
        make_company(name="Twin Company")
        report = A3PayrollImportService(
            build_a3_workbook(), company=self.company, allow_nif_mismatch=True
        ).validate()
        self.assertFalse(report.has_blocking_errors)
        self.assertFalse(report.needs_company_selection)
        self.assertEqual(report.company_id, self.company.id)
        self.assertEqual(report.matched_count, 2)
        self.assertTrue(report.can_commit)

    def test_manual_company_with_nif_mismatch_warns_instead_of_blocking(self):
        # Warnings interpolate lazy gettext strings at build time; pin the
        # language so the assertion is independent of LANGUAGE_CODE.
        with translation.override("en"):
            report = A3PayrollImportService(
                build_a3_workbook(nif="X99999999"),
                company=self.company,
                allow_nif_mismatch=True,
            ).validate()
            self.assertFalse(report.has_blocking_errors)
            self.assertEqual(report.company_id, self.company.id)
            self.assertTrue(any("does not match" in str(w) for w in report.warnings))
            self.assertTrue(report.can_commit)

    def test_nif_mismatch_still_blocks_without_override(self):
        report = A3PayrollImportService(
            build_a3_workbook(nif="X99999999"), company=self.company
        ).validate()
        self.assertTrue(report.has_blocking_errors)

    def test_duplicate_a3_code_is_blocking(self):
        report = A3PayrollImportService(build_a3_workbook(duplicate_code=True)).validate()
        self.assertTrue(report.has_blocking_errors)

    def test_ambiguous_name_not_matched(self):
        make_employee(
            company=self.company, first_name="JUAN", last_name="GARCIA LOPEZ",
            identification_number="DUP1",
        )
        report = A3PayrollImportService(build_a3_workbook()).validate()
        row = next(r for r in report.rows if r.a3_code == "000001")
        self.assertEqual(row.status, "ambiguous")

    def test_a3_code_on_profile_takes_priority(self):
        EmployeePayrollProfile.objects.create(employee=self.emp1, a3_employee_code="000001")
        report = A3PayrollImportService(build_a3_workbook()).validate()
        row = next(r for r in report.rows if r.a3_code == "000001")
        self.assertEqual(row.employee_id, self.emp1.id)

    def test_invalid_file_reports_error(self):
        report = A3PayrollImportService(BytesIO(b"not an excel")).validate()
        self.assertTrue(report.has_blocking_errors)


class CommitTests(A3ImporterTestBase):
    def test_commit_without_resolved_company_raises(self):
        with self.assertRaises(A3PayrollParseError):
            A3PayrollImportService(build_a3_workbook(nif="X99999999")).commit()

    def test_commit_with_manual_company(self):
        make_company(name="Twin Company")
        period = A3PayrollImportService(
            build_a3_workbook(), company=self.company, allow_nif_mismatch=True
        ).commit()
        self.assertEqual(period.company, self.company)
        self.assertEqual(period.payrolls.count(), 2)

    def test_commit_creates_payrolls_and_lines(self):
        period = A3PayrollImportService(build_a3_workbook(), source_file="march.xlsx").commit()
        self.assertEqual(period.company, self.company)
        self.assertEqual(period.source, PayrollPeriod.Source.EXCEL_IMPORT)
        self.assertEqual(period.payrolls.count(), 2)

        p1 = EmployeePayroll.objects.get(period=period, employee=self.emp1)
        self.assertEqual(p1.net_salary, Decimal("840.00"))
        self.assertEqual(p1.gross_salary, Decimal("1000.00"))
        self.assertEqual(p1.employer_cost, Decimal("1300.00"))
        self.assertEqual(p1.irpf_withholding_amount, Decimal("100.00"))
        self.assertEqual(p1.days_in_period, 30)
        # concept lines: salario base, ss 995, irpf 999
        self.assertEqual(p1.concept_lines.count(), 3)
        # SS breakdown for 995
        self.assertTrue(
            p1.ss_breakdown.filter(
                concept=PayrollSSBreakdown.SSConcept.COMMON_CONTINGENCIES
            ).exists()
        )

    def test_reimport_is_idempotent(self):
        A3PayrollImportService(build_a3_workbook()).commit()
        period = A3PayrollImportService(build_a3_workbook()).commit()
        self.assertEqual(period.payrolls.count(), 2)
        p1 = EmployeePayroll.objects.get(period=period, employee=self.emp1)
        # lines not duplicated
        self.assertEqual(p1.concept_lines.count(), 3)
        self.assertEqual(EmployeePayroll.objects.filter(employee=self.emp1).count(), 1)

    def test_closed_period_blocks_commit(self):
        period = A3PayrollImportService(build_a3_workbook()).commit()
        period.status = PayrollPeriod.Status.CLOSED
        period.save()
        with self.assertRaises(A3PayrollParseError):
            A3PayrollImportService(build_a3_workbook()).commit()

    def test_unknown_concept_is_auto_created(self):
        PayrollConcept.objects.filter(code="1").delete()
        A3PayrollImportService(build_a3_workbook()).commit()
        self.assertTrue(PayrollConcept.objects.filter(code="1").exists())


class PendingRowTests(A3ImporterTestBase):
    def _commit_with_unmatched(self):
        wb = build_a3_workbook(employees=[dict(MATCHED_EMP), dict(UNMATCHED_EMP)])
        return A3PayrollImportService(wb, source_file="march.xlsx").commit()

    def test_unmatched_employee_becomes_pending_row(self):
        period = self._commit_with_unmatched()
        self.assertEqual(period.payrolls.count(), 1)
        self.assertEqual(period.pending_rows.count(), 1)
        pending = period.pending_rows.get()
        self.assertEqual(pending.a3_code, "000003")
        self.assertEqual(pending.reason, PayrollImportPendingRow.Reason.UNMATCHED)
        self.assertEqual(pending.net_salary, Decimal("420.00"))
        self.assertIn("concepts", pending.payload)

    def test_resolve_pending_creates_payroll_and_sets_a3_code(self):
        period = self._commit_with_unmatched()
        pending = period.pending_rows.get()
        new_emp = make_employee(
            company=self.company, first_name="PEPE", last_name="NUEVO EMPLEADO",
            hired_at=date(2021, 1, 1),
        )

        payroll = resolve_pending_row(pending, new_emp)

        self.assertEqual(period.payrolls.count(), 2)
        self.assertEqual(period.pending_rows.count(), 0)
        self.assertEqual(payroll.net_salary, Decimal("420.00"))
        self.assertEqual(payroll.concept_lines.count(), 3)
        new_emp.refresh_from_db()
        self.assertEqual(new_emp.payroll_profile.a3_employee_code, "000003")

    def test_resolve_rejects_employee_from_other_company(self):
        period = self._commit_with_unmatched()
        pending = period.pending_rows.get()
        other = make_company(name="Other", tax_identifier="B99999999")
        outsider = make_employee(
            company=other, first_name="X", last_name="Y", hired_at=date(2021, 1, 1)
        )
        with self.assertRaises(A3PayrollParseError):
            resolve_pending_row(pending, outsider)

    def test_cannot_close_period_with_pending_rows(self):
        period = self._commit_with_unmatched()
        period.status = PayrollPeriod.Status.CLOSED
        with self.assertRaises(ValidationError):
            period.full_clean()

    def test_can_close_after_resolving_all_pending(self):
        period = self._commit_with_unmatched()
        pending = period.pending_rows.get()
        new_emp = make_employee(
            company=self.company, first_name="PEPE", last_name="NUEVO EMPLEADO",
            hired_at=date(2021, 1, 1),
        )
        resolve_pending_row(pending, new_emp)

        period.status = PayrollPeriod.Status.CLOSED
        period.full_clean()  # should not raise
        period.save()
        self.assertEqual(period.status, PayrollPeriod.Status.CLOSED)

    def test_reimport_after_resolution_keeps_match(self):
        period = self._commit_with_unmatched()
        pending = period.pending_rows.get()
        new_emp = make_employee(
            company=self.company, first_name="PEPE", last_name="NUEVO EMPLEADO",
            hired_at=date(2021, 1, 1),
        )
        resolve_pending_row(pending, new_emp)

        # Re-importing now matches via the saved A3 code: no pending rows remain.
        wb = build_a3_workbook(employees=[dict(MATCHED_EMP), dict(UNMATCHED_EMP)])
        period = A3PayrollImportService(wb).commit()
        self.assertEqual(period.pending_rows.count(), 0)
        self.assertEqual(period.payrolls.count(), 2)
