"""
Importer for A3 payroll Excel exports (one file per company per month).

Two layouts are supported and auto-detected:

* **Pivoted matrix** (``.xlsx``): employees are *columns*, payroll concepts /
  totals are *rows*. Employees are matched by A3 code (profile) then name.
* **Tabular** (``.xls``): one *row* per employee, fields in *columns*. This
  layout carries the DNI, so employees are matched by DNI (more reliable).

Both layouts are parsed into a common intermediate (``ParsedFile`` +
``EmployeeRow`` with a precomputed ``payload``), after which matching,
validation and commit are identical.

Design: two phases.
    * ``validate()`` runs a full dry-run (no DB writes) and returns an
      ``ImportReport`` with blocking errors, warnings and a per-employee
      preview. Used to render a confirmation screen.
    * ``commit()`` re-validates and, if there are no blocking errors,
      writes everything inside a single atomic transaction (idempotent:
      re-importing a draft period updates in place without duplicating).
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from io import BytesIO
from typing import Optional

import openpyxl
from django.db import transaction
from django.utils.translation import gettext_lazy as _

from company.models import Company, Employee
from payroll.models import (
    EmployeePayroll,
    PayrollConcept,
    PayrollConceptLine,
    PayrollPeriod,
    PayrollSSBreakdown,
)


TWO_PLACES = Decimal("0.01")
RECONCILE_TOLERANCE = Decimal("0.02")

# ── Pivoted layout: normalized summary-row label -> EmployeePayroll field(s) ──
SUMMARY_FIELD_MAP = {
    "TOTAL BRUTO": ("gross_salary", "total_accrued"),
    "TOTAL DEDUCCIONES": ("total_deducted",),
    "TOTAL LIQUIDO": ("net_salary",),
    "PRORRATA PAGAS EXTRAS": ("prorated_extra_payments",),
    "SEGURIDAD SOCIAL EMPRESA": ("employer_social_security",),
    "SEGURIDAD SOCIAL TRABAJADOR": ("employee_social_security",),
    "COSTE EMPRESA": ("employer_cost",),
    "BASE CONTINGENCIAS COMUNES": ("social_security_base",),
    "BASE ACCIDENTES": ("workplace_accident_base",),
    "BASE IRPF DINERARIA": ("irpf_base",),
}

# ── Tabular layout: normalized column header -> EmployeePayroll field(s) ──
TABULAR_SUMMARY_MAP = {
    "TOT BRUTO": ("gross_salary", "total_accrued"),
    "TOT DEDUC": ("total_deducted",),
    "TOTAL LIQ": ("net_salary",),
    "P P EXTRAS": ("prorated_extra_payments",),
    "SS EMPRESA": ("employer_social_security",),
    "SS TRAB": ("employee_social_security",),
    "COSTE EMP": ("employer_cost",),
    "BASE CC": ("social_security_base",),
    "BASE ACC": ("workplace_accident_base",),
    "B IRPF DIN": ("irpf_base",),
}
TABULAR_ESPECIES_LABEL = "B IRPF ESP"
TABULAR_DAYS_LABEL = "DIAS COT"
TABULAR_DNI_LABEL = "DNI"
TABULAR_NAME_LABEL = "APELLIDOS Y NOMBRE"
TABULAR_GROSS_LABEL = "TOT BRUTO"
TABULAR_DEDUC_LABEL = "TOT DEDUC"

# A3 concept code -> Social Security breakdown concept (codes are normalized)
SS_CONCEPT_MAP = {
    "995": PayrollSSBreakdown.SSConcept.COMMON_CONTINGENCIES,
    "996": PayrollSSBreakdown.SSConcept.VOCATIONAL_TRAINING,
    "997": PayrollSSBreakdown.SSConcept.UNEMPLOYMENT,
}

IRPF_CONCEPT_CODE = "999"
GROSS_TOTAL_LABEL = "TOTAL BRUTO"
DEDUCTIONS_TOTAL_LABEL = "TOTAL DEDUCCIONES"
IRPF_ESPECIES_LABEL = "BASE IRPF ESPECIES"
CONCEPT_HEADER_LABEL = "CONCEPTO"

FIRST_DATA_COL = 3  # column D (0-indexed) — first employee column (pivoted)

_CONCEPT_HEADER_RE = re.compile(r"^\s*(\d{3,4})\s*[-\s]\s*(.+)$")


def _strip_accents(text: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c)
    )


def normalize_label(value) -> str:
    """Uppercase, accent-free, period-free, single-spaced label."""
    if value is None:
        return ""
    text = _strip_accents(str(value)).upper()
    text = text.replace(".", " ")
    return re.sub(r"\s+", " ", text).strip()


def normalize_name(*parts) -> str:
    """Order-independent normalized name key (sorted tokens)."""
    tokens = []
    for part in parts:
        if not part:
            continue
        cleaned = _strip_accents(str(part)).upper()
        cleaned = re.sub(r"[^A-Z0-9 ]", " ", cleaned)
        tokens.extend(t for t in cleaned.split() if t)
    return " ".join(sorted(tokens))


def normalize_nif(value) -> str:
    if value is None:
        return ""
    return re.sub(r"[^A-Z0-9]", "", str(value).upper())


def normalize_concept_code(code) -> str:
    """Strip leading zeros so '0995' and '995' map to the same concept."""
    c = re.sub(r"\D", "", str(code or ""))
    return c.lstrip("0") or "0"


def to_decimal(value) -> Decimal:
    """Convert an Excel cell to a 2-decimal Decimal, tolerating float noise."""
    if value is None or value == "":
        return Decimal("0.00")
    try:
        return Decimal(str(value)).quantize(TWO_PLACES)
    except (InvalidOperation, ValueError, TypeError):
        return Decimal("0.00")


@dataclass
class EmployeeRow:
    a3_code: str = ""
    dni: str = ""
    surname1: str = ""
    surname2: str = ""
    given_name: str = ""
    column: Optional[int] = None  # pivoted layout only
    # "pending" until matching runs (it is skipped while the company is
    # unresolved, e.g. duplicate NIF awaiting manual selection).
    status: str = "pending"  # pending | ok | unmatched | ambiguous | duplicate
    employee_id: Optional[int] = None
    employee_label: str = ""
    messages: list = field(default_factory=list)
    net_salary: Decimal = Decimal("0.00")
    gross_salary: Decimal = Decimal("0.00")
    employer_cost: Decimal = Decimal("0.00")
    payload: dict = field(default_factory=dict)

    @property
    def full_name(self) -> str:
        parts = [p for p in [self.surname1, self.surname2, self.given_name] if p]
        return " ".join(parts)

    @property
    def is_importable(self) -> bool:
        return self.status == "ok" and self.employee_id is not None


@dataclass
class ParsedFile:
    nif: str = ""
    company_name: str = ""
    period_start: Optional[date] = None
    period_end: Optional[date] = None
    employees: list = field(default_factory=list)
    layout: str = ""
    file_total_net: Optional[Decimal] = None
    sheet2_total_liquido: Optional[Decimal] = None
    sheet2_total_workers: Optional[int] = None


@dataclass
class ImportReport:
    company_id: Optional[int] = None
    company_name: str = ""
    nif: str = ""
    year: Optional[int] = None
    month: Optional[int] = None
    period_start: Optional[date] = None
    period_end: Optional[date] = None
    existing_period_status: Optional[str] = None
    source_file: str = ""
    layout: str = ""
    rows: list = field(default_factory=list)
    errors: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    sheet2_total_liquido: Optional[Decimal] = None
    sheet2_total_workers: Optional[int] = None
    file_total_net: Optional[Decimal] = None
    needs_company_selection: bool = False
    company_candidate_ids: list = field(default_factory=list)

    @property
    def has_blocking_errors(self) -> bool:
        return bool(self.errors)

    @property
    def can_commit(self) -> bool:
        # Unmatched employees are stored as pending rows rather than blocking
        # the import, so we only require a parseable file with at least one row
        # and a resolved company (picked manually when the NIF is ambiguous).
        return (
            not self.has_blocking_errors
            and bool(self.rows)
            and self.company_id is not None
        )

    @property
    def matched_count(self) -> int:
        return sum(1 for r in self.rows if r.is_importable)

    @property
    def unmatched_count(self) -> int:
        return sum(1 for r in self.rows if not r.is_importable)

    @property
    def total_liquido(self) -> Decimal:
        return sum((r.net_salary for r in self.rows if r.is_importable), Decimal("0.00"))


class A3PayrollParseError(Exception):
    """Raised when the file does not look like an A3 payroll export."""


class A3PayrollImportService:
    def __init__(
        self,
        file,
        *,
        company: Optional[Company] = None,
        source_file: str = "",
        allow_nif_mismatch: bool = False,
    ):
        self.file = file
        self.expected_company = company
        self.source_file = source_file
        # True when ``company`` was picked manually by the user (NIF missing,
        # unknown or duplicated): a NIF mismatch is then a warning, not an error.
        self.allow_nif_mismatch = allow_nif_mismatch
        self._parsed: Optional[ParsedFile] = None

    # ── Reading workbooks (xls/xlsx, content-sniffed) ──────────

    def _read_bytes(self) -> bytes:
        if hasattr(self.file, "seek"):
            try:
                self.file.seek(0)
            except (OSError, ValueError):
                pass
        data = self.file.read()
        if isinstance(data, str):
            data = data.encode("latin-1", "ignore")
        return data

    def _read_workbook(self, data):
        """Return ``(rows, (sheet2_liquido, sheet2_workers))``."""
        head = data[:8]
        if head[:2] == b"PK":
            return self._read_xlsx(data)
        if head[:4] == b"\xd0\xcf\x11\xe0":
            return self._read_xls(data), (None, None)
        # Unknown magic: try xlsx, then xls.
        try:
            return self._read_xlsx(data)
        except A3PayrollParseError:
            return self._read_xls(data), (None, None)

    def _read_xlsx(self, data):
        try:
            wb = openpyxl.load_workbook(BytesIO(data), read_only=True, data_only=True)
        except Exception as exc:  # noqa: BLE001 - surface a clean message
            raise A3PayrollParseError(
                _("The file could not be read as an Excel workbook (.xlsx).")
            ) from exc
        detail = wb[wb.sheetnames[0]]
        rows = [tuple(r) for r in detail.iter_rows(values_only=True)]
        liquido = workers = None
        if len(wb.sheetnames) > 1:
            liquido, workers = self._read_sheet2_totals(wb[wb.sheetnames[1]])
        wb.close()
        return rows, (liquido, workers)

    def _read_xls(self, data):
        try:
            import xlrd

            wb = xlrd.open_workbook(file_contents=data)
        except Exception as exc:  # noqa: BLE001
            raise A3PayrollParseError(
                _("The file could not be read as an Excel workbook (.xls).")
            ) from exc
        sheet = wb.sheet_by_index(0)
        return [tuple(sheet.row_values(r)) for r in range(sheet.nrows)]

    @staticmethod
    def _read_sheet2_totals(sheet):
        liquido = None
        workers = None
        for row in sheet.iter_rows(values_only=True):
            label = normalize_label(row[0]) if row else ""
            if not label:
                continue
            value = row[3] if len(row) > 3 else None
            if label == "TOTAL LIQUIDO":
                liquido = to_decimal(value)
            elif label == "TOTAL TRABAJADORES" and value is not None:
                try:
                    workers = int(value)
                except (TypeError, ValueError):
                    workers = None
        return liquido, workers

    # ── Format detection + dispatch ────────────────────────────

    def _get_parsed(self) -> ParsedFile:
        if self._parsed is None:
            data = self._read_bytes()
            rows, sheet2 = self._read_workbook(data)
            if self._detect_layout(rows) == "tabular":
                self._parsed = self._parse_tabular(rows)
            else:
                self._parsed = self._parse_pivoted(rows, sheet2)
        return self._parsed

    @staticmethod
    def _detect_layout(rows) -> str:
        for row in rows:
            cells = {normalize_label(c) for c in row}
            if TABULAR_DNI_LABEL in cells and TABULAR_NAME_LABEL in cells:
                return "tabular"
        return "pivoted"

    @staticmethod
    def _find_company_period(rows):
        dates_idx = company_idx = None
        for idx, row in enumerate(rows):
            label = normalize_label(row[0]) if row else ""
            if dates_idx is None and label.startswith("DEL "):
                dates_idx = idx
            if company_idx is None and label.startswith("EMPRESA"):
                company_idx = idx
        if dates_idx is None or company_idx is None:
            raise A3PayrollParseError(
                _("This does not look like an A3 payroll export (missing period/company header).")
            )
        period_start, period_end = A3PayrollImportService._parse_period(rows[dates_idx][0])
        nif = A3PayrollImportService._parse_nif(rows[company_idx][0])
        name = A3PayrollImportService._parse_company_name(rows[company_idx][0])
        return period_start, period_end, nif, name, dates_idx, company_idx

    @staticmethod
    def _parse_period(label):
        text = str(label or "")
        matches = re.findall(r"(\d{2})/(\d{2})/(\d{2,4})", text)
        if not matches:
            raise A3PayrollParseError(_("Could not parse the payroll period dates."))

        def build(match):
            day, month, year = (int(p) for p in match)
            if year < 100:
                year += 2000
            return date(year, month, day)

        start = build(matches[0])
        end = build(matches[1]) if len(matches) > 1 else start
        return start, end

    @staticmethod
    def _parse_nif(label):
        text = str(label or "")
        match = re.search(r"NIF[:\s]*([A-Za-z0-9]+)", text, re.IGNORECASE)
        return normalize_nif(match.group(1)) if match else ""

    @staticmethod
    def _parse_company_name(label):
        text = str(label or "")
        text = re.sub(r"^\s*EMPRESA\s*:\s*", "", text, flags=re.IGNORECASE)
        text = re.split(r"\s+NIF[:\s]", text, flags=re.IGNORECASE)[0]
        text = re.sub(r"^\d+\s*-\s*", "", text)
        return text.strip()

    @staticmethod
    def _attach_payload(emp: EmployeeRow, payload: dict):
        emp.payload = payload
        summary = payload.get("summary", {})
        emp.net_salary = Decimal(summary.get("net_salary", "0.00"))
        emp.gross_salary = Decimal(summary.get("gross_salary", "0.00"))
        emp.employer_cost = Decimal(summary.get("employer_cost", "0.00"))

    # ── Pivoted parser ─────────────────────────────────────────

    def _parse_pivoted(self, rows, sheet2) -> ParsedFile:
        period_start, period_end, nif, name, dates_idx, company_idx = (
            self._find_company_period(rows)
        )
        employees = self._parse_employee_columns(rows, dates_idx, company_idx)
        last_col = max((e.column for e in employees), default=FIRST_DATA_COL - 1)
        indexed = self._index_rows(rows)
        for emp in employees:
            payload = self._extract_pivoted_payload(
                emp.column, indexed, period_start, period_end
            )
            self._attach_payload(emp, payload)
        liquido, workers = sheet2
        return ParsedFile(
            nif=nif,
            company_name=name,
            period_start=period_start,
            period_end=period_end,
            employees=employees,
            layout="pivoted",
            file_total_net=self._find_file_total(rows, "TOTAL LIQUIDO", last_col),
            sheet2_total_liquido=liquido,
            sheet2_total_workers=workers,
        )

    @staticmethod
    def _parse_employee_columns(rows, dates_row_idx, company_row_idx):
        code_row = rows[dates_row_idx]
        surname1_row = rows[company_row_idx] if company_row_idx < len(rows) else ()
        surname2_row = rows[company_row_idx + 1] if company_row_idx + 1 < len(rows) else ()
        given_row = rows[company_row_idx + 2] if company_row_idx + 2 < len(rows) else ()

        def cell(row, col):
            return row[col] if row and col < len(row) and row[col] is not None else ""

        employees = []
        for col in range(FIRST_DATA_COL, len(code_row)):
            raw_code = cell(code_row, col)
            if raw_code == "" or raw_code is None:
                continue
            employees.append(
                EmployeeRow(
                    column=col,
                    a3_code=str(raw_code).strip(),
                    surname1=str(cell(surname1_row, col)).strip(),
                    surname2=str(cell(surname2_row, col)).strip(),
                    given_name=str(cell(given_row, col)).strip(),
                )
            )
        return employees

    @staticmethod
    def _find_file_total(rows, label, after_col):
        """Read the file's own TOTAL column value for ``label`` (the trailing
        column past the last employee that A3 fills with row totals)."""
        target = normalize_label(label)
        for row in rows:
            if not row or normalize_label(row[0]) != target:
                continue
            for col in range(after_col + 1, len(row)):
                if row[col] is not None and str(row[col]).strip() != "":
                    return to_decimal(row[col])
        return None

    def _index_rows(self, rows):
        """Return structured row info: concepts (code/name/section/values) and summaries."""
        concepts = []
        summaries = []
        section = "accrual"
        for row in rows:
            label = normalize_label(row[0]) if row else ""
            code = row[1] if len(row) > 1 and row[1] is not None else None

            if label == GROSS_TOTAL_LABEL:
                section = "deduction"
            elif label == DEDUCTIONS_TOTAL_LABEL:
                section = "summary"

            if code is not None and str(code).strip() != "":
                concepts.append(
                    {
                        "code": normalize_concept_code(code),
                        "name": str(row[2]).strip() if len(row) > 2 and row[2] else "",
                        "section": section,
                        "row": row,
                    }
                )
            elif label in SUMMARY_FIELD_MAP or label == IRPF_ESPECIES_LABEL or label == "DIAS COTIZADOS":
                summaries.append({"label": label, "row": row})
        return {"concepts": concepts, "summaries": summaries}

    @staticmethod
    def _extract_pivoted_payload(col, indexed, period_start, period_end):
        raw_data = {}
        summary_fields = {}
        days_in_period = None
        irpf_especies = Decimal("0.00")

        for item in indexed["summaries"]:
            label = item["label"]
            value = item["row"][col] if col < len(item["row"]) else None
            dec = to_decimal(value)
            raw_data[label] = str(dec)
            if label == "DIAS COTIZADOS":
                days_in_period = int(dec) if dec > 0 else None
            elif label == IRPF_ESPECIES_LABEL:
                irpf_especies = dec
            else:
                for field_name in SUMMARY_FIELD_MAP.get(label, ()):  # noqa: PD011
                    summary_fields[field_name] = str(dec)

        concepts = []
        for concept_info in indexed["concepts"]:
            value = concept_info["row"][col] if col < len(concept_info["row"]) else None
            dec = to_decimal(value)
            if dec == 0:
                continue
            code = concept_info["code"]
            raw_data[f"concept_{code}"] = str(dec)
            concepts.append(
                {
                    "code": code,
                    "name": concept_info["name"],
                    "section": concept_info["section"],
                    "amount": str(dec),
                }
            )

        return {
            "period_start": period_start.isoformat(),
            "period_end": period_end.isoformat(),
            "days_in_period": days_in_period,
            "irpf_especies": str(irpf_especies),
            "summary": summary_fields,
            "concepts": concepts,
            "raw_data": raw_data,
        }

    # ── Tabular parser ─────────────────────────────────────────

    def _parse_tabular(self, rows) -> ParsedFile:
        period_start, period_end, nif, name, _dates_idx, _company_idx = (
            self._find_company_period(rows)
        )

        header_idx = None
        for idx, row in enumerate(rows):
            cells = {normalize_label(c) for c in row}
            if TABULAR_DNI_LABEL in cells and TABULAR_NAME_LABEL in cells:
                header_idx = idx
                break
        if header_idx is None:
            raise A3PayrollParseError(_("Could not find the employee header row."))

        header = rows[header_idx]
        col_by_label = {}
        for c, raw in enumerate(header):
            label = normalize_label(raw)
            if label and label not in col_by_label:
                col_by_label[label] = c

        dni_col = col_by_label.get(TABULAR_DNI_LABEL)
        name_col = col_by_label.get(TABULAR_NAME_LABEL)
        days_col = col_by_label.get(TABULAR_DAYS_LABEL)
        especies_col = col_by_label.get(TABULAR_ESPECIES_LABEL)
        gross_col = col_by_label.get(TABULAR_GROSS_LABEL)
        deduc_col = col_by_label.get(TABULAR_DEDUC_LABEL)

        summary_cols = [
            (col_by_label[label], fields)
            for label, fields in TABULAR_SUMMARY_MAP.items()
            if label in col_by_label
        ]

        concept_cols = []
        for c, raw in enumerate(header):
            match = _CONCEPT_HEADER_RE.match(str(raw or ""))
            if not match:
                continue
            code = normalize_concept_code(match.group(1))
            cname = match.group(2).strip()
            if gross_col is not None and c < gross_col:
                section = "accrual"
            else:
                section = "deduction"
            concept_cols.append((c, code, cname, section))

        employees = []
        for r in range(header_idx + 1, len(rows)):
            row = rows[r]
            dni_raw = row[dni_col] if dni_col is not None and dni_col < len(row) else ""
            if not str(dni_raw).strip():
                continue
            full_name = (
                str(row[name_col]).strip()
                if name_col is not None and name_col < len(row)
                else ""
            )
            surname, given = self._split_name(full_name)
            payload = self._build_tabular_payload(
                row, period_start, period_end,
                summary_cols, especies_col, days_col, concept_cols,
            )
            emp = EmployeeRow(
                a3_code="",
                dni=normalize_nif(dni_raw),
                surname1=surname,
                given_name=given,
            )
            self._attach_payload(emp, payload)
            employees.append(emp)

        return ParsedFile(
            nif=nif,
            company_name=name,
            period_start=period_start,
            period_end=period_end,
            employees=employees,
            layout="tabular",
        )

    @staticmethod
    def _split_name(full_name):
        """'APELLIDOS, NOMBRE' -> ('APELLIDOS', 'NOMBRE')."""
        if "," in full_name:
            surnames, _, given = full_name.partition(",")
            return surnames.strip(), given.strip()
        return full_name.strip(), ""

    @staticmethod
    def _build_tabular_payload(row, period_start, period_end,
                               summary_cols, especies_col, days_col, concept_cols):
        def val(col):
            return row[col] if col is not None and col < len(row) else None

        raw_data = {}
        summary_fields = {}
        for col, fields in summary_cols:
            dec = to_decimal(val(col))
            for field_name in fields:
                summary_fields[field_name] = str(dec)

        days_in_period = None
        if days_col is not None:
            dec = to_decimal(val(days_col))
            days_in_period = int(dec) if dec > 0 else None

        irpf_especies = to_decimal(val(especies_col)) if especies_col is not None else Decimal("0.00")

        concepts = []
        for col, code, cname, section in concept_cols:
            dec = to_decimal(val(col))
            if dec == 0:
                continue
            raw_data[f"concept_{code}"] = str(dec)
            concepts.append(
                {"code": code, "name": cname, "section": section, "amount": str(dec)}
            )

        return {
            "period_start": period_start.isoformat(),
            "period_end": period_end.isoformat(),
            "days_in_period": days_in_period,
            "irpf_especies": str(irpf_especies),
            "summary": summary_fields,
            "concepts": concepts,
            "raw_data": raw_data,
        }

    # ── Validation ─────────────────────────────────────────────

    def validate(self) -> ImportReport:
        report = ImportReport(source_file=self.source_file)
        try:
            parsed = self._get_parsed()
        except A3PayrollParseError as exc:
            report.errors.append(str(exc))
            return report

        report.layout = parsed.layout
        report.nif = parsed.nif
        report.company_name = parsed.company_name
        report.period_start = parsed.period_start
        report.period_end = parsed.period_end
        report.year = parsed.period_start.year
        report.month = parsed.period_start.month
        report.sheet2_total_liquido = parsed.sheet2_total_liquido
        report.sheet2_total_workers = parsed.sheet2_total_workers
        report.file_total_net = parsed.file_total_net

        company = self._resolve_company(parsed.nif, report)
        if company is not None:
            report.company_id = company.id
            report.company_name = company.name
            self._check_existing_period(company, report)

        self._detect_file_duplicates(parsed.employees, report)
        if company is not None:
            self._match_employees(company, parsed.employees, report)

        report.rows = parsed.employees
        self._reconcile_totals(report)
        return report

    def _resolve_company(self, nif, report) -> Optional[Company]:
        if self.expected_company is not None:
            if nif and normalize_nif(self.expected_company.tax_identifier) != nif:
                if self.allow_nif_mismatch:
                    report.warnings.append(
                        _(
                            "The file NIF (%(file)s) does not match the NIF of "
                            "%(company)s; importing into it anyway."
                        )
                        % {"file": nif, "company": self.expected_company.name}
                    )
                else:
                    report.errors.append(
                        _("The file NIF (%(file)s) does not match the selected company.")
                        % {"file": nif}
                    )
                    return None
            return self.expected_company

        if not nif:
            report.needs_company_selection = True
            report.warnings.append(
                _("No company NIF found in the file header. Select the company manually.")
            )
            return None

        matches = [
            c for c in Company.objects.all() if normalize_nif(c.tax_identifier) == nif
        ]
        if not matches:
            report.needs_company_selection = True
            report.warnings.append(
                _("No company found with NIF %(nif)s. Select the company to import into.")
                % {"nif": nif}
            )
            return None
        if len(matches) > 1:
            report.needs_company_selection = True
            report.company_candidate_ids = [c.id for c in matches]
            report.warnings.append(
                _("Multiple companies share NIF %(nif)s. Select the correct one.")
                % {"nif": nif}
            )
            return None
        return matches[0]

    def _check_existing_period(self, company, report):
        existing = (
            PayrollPeriod.objects.filter(
                company=company, year=report.year, month=report.month
            )
            .values_list("status", flat=True)
            .first()
        )
        report.existing_period_status = existing
        if existing == PayrollPeriod.Status.CLOSED:
            report.errors.append(
                _("Payroll period %(m)02d/%(y)d is closed. Reopen it before re-importing.")
                % {"m": report.month, "y": report.year}
            )
        elif existing == PayrollPeriod.Status.DRAFT:
            report.warnings.append(
                _("A draft period for %(m)02d/%(y)d already exists; it will be updated.")
                % {"m": report.month, "y": report.year}
            )

    @staticmethod
    def _detect_file_duplicates(employees, report):
        """Flag rows that share the same DNI or A3 code within the file."""
        groups = {}
        for emp in employees:
            if emp.dni:
                groups.setdefault(("DNI", emp.dni), []).append(emp)
            if emp.a3_code:
                groups.setdefault(("A3", emp.a3_code), []).append(emp)
        for (kind, key), group in groups.items():
            if len(group) > 1:
                for emp in group:
                    emp.status = "duplicate"
                    emp.messages.append(_("Duplicate %(kind)s in file") % {"kind": kind})
                report.errors.append(
                    _("%(kind)s %(key)s appears %(n)d times in the file.")
                    % {"kind": kind, "key": key, "n": len(group)}
                )

    def _match_employees(self, company, employees, report):
        from payroll.models import EmployeePayrollProfile

        code_to_employee = {}
        for prof in EmployeePayrollProfile.objects.filter(
            employee__company=company
        ).exclude(a3_employee_code="").values("a3_employee_code", "employee_id"):
            code_to_employee[prof["a3_employee_code"].strip()] = prof["employee_id"]

        dni_index = {}
        name_index = {}
        employee_labels = {}
        for emp in Employee.objects.filter(company=company):
            dni = normalize_nif(emp.identification_number)
            if dni:
                dni_index.setdefault(dni, []).append(emp.id)
            key = normalize_name(emp.first_name, emp.last_name)
            name_index.setdefault(key, []).append(emp.id)
            employee_labels[emp.id] = f"{emp.first_name} {emp.last_name}".strip()

        used_employee_ids = set()
        for emp in employees:
            if emp.status == "duplicate":
                continue

            # 1. DNI (most reliable; unique per company). When the file
            # carries a DNI we trust it exclusively: we never fall back to a
            # name match, to avoid assigning payroll to the wrong person.
            if emp.dni:
                candidates = dni_index.get(emp.dni, [])
                if len(candidates) == 1:
                    self._assign_match(emp, candidates[0], employee_labels, used_employee_ids, report)
                elif len(candidates) > 1:
                    emp.status = "ambiguous"
                    emp.messages.append(_("Multiple employees share this DNI"))
                else:
                    emp.status = "unmatched"
                    emp.messages.append(
                        _("No employee with DNI %(dni)s") % {"dni": emp.dni}
                    )
                continue

            # 2. A3 code on the payroll profile.
            matched_id = code_to_employee.get(emp.a3_code) if emp.a3_code else None
            if matched_id is not None:
                self._assign_match(emp, matched_id, employee_labels, used_employee_ids, report)
                continue

            # 3. Name (fallback).
            key = normalize_name(emp.surname1, emp.surname2, emp.given_name)
            candidates = name_index.get(key, [])
            if len(candidates) == 1:
                emp.messages.append(_("Matched by name"))
                self._assign_match(emp, candidates[0], employee_labels, used_employee_ids, report)
            elif len(candidates) > 1:
                emp.status = "ambiguous"
                emp.messages.append(_("Multiple employees match this name"))
            else:
                emp.status = "unmatched"
                if emp.dni:
                    emp.messages.append(
                        _("No employee with DNI %(dni)s") % {"dni": emp.dni}
                    )
                else:
                    emp.messages.append(_("No employee found (set A3 code or DNI)"))

    @staticmethod
    def _assign_match(emp, employee_id, employee_labels, used_employee_ids, report):
        if employee_id in used_employee_ids:
            emp.status = "duplicate"
            emp.messages.append(_("Two rows resolve to the same employee"))
            report.errors.append(
                _("Employee %(name)s is matched by more than one row.")
                % {"name": employee_labels.get(employee_id, employee_id)}
            )
            return
        emp.status = "ok"
        emp.employee_id = employee_id
        emp.employee_label = employee_labels.get(employee_id, "")
        used_employee_ids.add(employee_id)

    def _reconcile_totals(self, report):
        if report.sheet2_total_workers is not None:
            parsed = len(report.rows)
            if parsed != report.sheet2_total_workers:
                report.warnings.append(
                    _("Worker count mismatch: file says %(s)d, parsed %(p)d.")
                    % {"s": report.sheet2_total_workers, "p": parsed}
                )
        if report.file_total_net is not None:
            parsed_total = sum((r.net_salary for r in report.rows), Decimal("0.00"))
            if abs(parsed_total - report.file_total_net) > RECONCILE_TOLERANCE:
                report.warnings.append(
                    _("Net total mismatch: file total column says %(s)s, parsed %(p)s.")
                    % {"s": report.file_total_net, "p": parsed_total}
                )

    # ── Commit ─────────────────────────────────────────────────

    @transaction.atomic
    def commit(self) -> PayrollPeriod:
        report = self.validate()
        if report.has_blocking_errors:
            raise A3PayrollParseError(
                _("Cannot import: %(err)s") % {"err": "; ".join(str(e) for e in report.errors)}
            )
        if report.company_id is None:
            raise A3PayrollParseError(
                _("The company for this file could not be resolved. Select one first.")
            )

        company = Company.objects.get(pk=report.company_id)
        period, _created = PayrollPeriod.objects.update_or_create(
            company=company,
            year=report.year,
            month=report.month,
            defaults={"source": PayrollPeriod.Source.EXCEL_IMPORT},
        )
        if period.status == PayrollPeriod.Status.CLOSED:
            raise A3PayrollParseError(_("Period is closed."))

        concept_cache = {}
        # Rebuild pending rows from the current parse (idempotent re-import).
        period.pending_rows.all().delete()

        for emp in report.rows:
            if emp.is_importable:
                payroll, _created = EmployeePayroll.objects.update_or_create(
                    period=period,
                    employee_id=emp.employee_id,
                    defaults={"company": company, "source_file": self.source_file},
                )
                apply_payload(payroll, emp.payload, concept_cache)
            elif emp.status in ("unmatched", "ambiguous"):
                self._create_pending_row(period, emp)

        return period

    def _create_pending_row(self, period, emp):
        from payroll.models import PayrollImportPendingRow

        PayrollImportPendingRow.objects.update_or_create(
            period=period,
            a3_code=emp.a3_code,
            dni=emp.dni,
            defaults={
                "full_name": emp.full_name,
                "surname1": emp.surname1,
                "surname2": emp.surname2,
                "given_name": emp.given_name,
                "reason": emp.status,
                "net_salary": emp.net_salary,
                "gross_salary": emp.gross_salary,
                "employer_cost": emp.employer_cost,
                "payload": emp.payload,
                "source_file": self.source_file,
            },
        )


def _parse_iso_date(value):
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def apply_payload(payroll, payload, concept_cache=None):
    """Populate (and save) ``payroll`` from a parsed payload.

    Shared by the importer (matched employees) and pending-row resolution
    (assigning a stored payload to a chosen employee), so a resolved row
    produces exactly the same record an import would have.
    """
    if concept_cache is None:
        concept_cache = {}

    payroll.period_start = _parse_iso_date(payload.get("period_start"))
    payroll.period_end = _parse_iso_date(payload.get("period_end"))

    # Reset child rows for idempotency
    payroll.concept_lines.all().delete()
    payroll.ss_breakdown.all().delete()

    if payload.get("days_in_period"):
        payroll.days_in_period = payload["days_in_period"]
    for field_name, value in payload.get("summary", {}).items():
        setattr(payroll, field_name, Decimal(value))

    for concept in payload.get("concepts", []):
        dec = Decimal(concept["amount"])
        code = concept["code"]
        concept_obj = _get_or_create_concept(
            code, concept["name"], concept["section"], concept_cache
        )
        PayrollConceptLine.objects.create(payroll=payroll, concept=concept_obj, amount=dec)
        if code in SS_CONCEPT_MAP:
            PayrollSSBreakdown.objects.create(
                payroll=payroll,
                concept=SS_CONCEPT_MAP[code],
                base=payroll.social_security_base,
                employee_amount=dec,
            )
        if code == IRPF_CONCEPT_CODE:
            payroll.irpf_withholding_amount = dec

    # Derived IRPF base (dineraria + especies) and percentage
    payroll.irpf_base = payroll.irpf_base + Decimal(payload.get("irpf_especies", "0.00"))
    if payroll.irpf_base > 0:
        payroll.irpf_percentage = (
            payroll.irpf_withholding_amount / payroll.irpf_base * 100
        ).quantize(TWO_PLACES)

    _enrich_from_profile(payroll)

    payroll.raw_data = payload.get("raw_data", {})
    payroll.save()


def _enrich_from_profile(payroll):
    profile = getattr(payroll.employee, "payroll_profile", None)
    if profile is None:
        return
    if not payroll.iban and profile.iban:
        payroll.iban = profile.iban
    if not payroll.social_security_affiliation_number and profile.social_security_affiliation_number:
        payroll.social_security_affiliation_number = (
            profile.social_security_affiliation_number
        )


def _get_or_create_concept(code, name, section, cache):
    if code in cache:
        return cache[code]
    concept_type = (
        PayrollConcept.ConceptType.DEDUCTION
        if section == "deduction"
        else PayrollConcept.ConceptType.ACCRUAL
    )
    concept, _created = PayrollConcept.objects.get_or_create(
        code=code,
        defaults={"name": name or code, "type": concept_type},
    )
    cache[code] = concept
    return concept


@transaction.atomic
def resolve_pending_row(pending_row, employee, *, set_a3_code=True):
    """Turn a pending row into a real ``EmployeePayroll`` for ``employee``.

    Records the A3 code and/or DNI on the employee (and profile) so future
    imports match automatically. The pending row is deleted on success.
    """
    period = pending_row.period
    if employee.company_id != period.company_id:
        raise A3PayrollParseError(
            _("Employee %(name)s does not belong to this period's company.")
            % {"name": employee}
        )

    payroll, _created = EmployeePayroll.objects.update_or_create(
        period=period,
        employee=employee,
        defaults={"company": period.company, "source_file": pending_row.source_file},
    )
    apply_payload(payroll, pending_row.payload)

    if set_a3_code and pending_row.a3_code:
        from payroll.models import EmployeePayrollProfile

        profile, _created = EmployeePayrollProfile.objects.get_or_create(employee=employee)
        if not profile.a3_employee_code:
            profile.a3_employee_code = pending_row.a3_code
            profile.save(update_fields=["a3_employee_code"])

    # Backfill the DNI so the next import matches this employee automatically.
    if pending_row.dni and not normalize_nif(employee.identification_number):
        clash = (
            Employee.objects.filter(company_id=period.company_id)
            .exclude(pk=employee.pk)
            .filter(identification_number=pending_row.dni)
            .exists()
        )
        if not clash:
            employee.identification_number = pending_row.dni
            employee.save(update_fields=["identification_number"])

    pending_row.delete()
    return payroll
