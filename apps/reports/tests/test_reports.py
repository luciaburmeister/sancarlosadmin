from datetime import date, datetime, timedelta, timezone as dt_timezone
from decimal import Decimal
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.contrib.contenttypes.models import ContentType
from django.core.cache import cache
from django.core.exceptions import PermissionDenied
from django.test import TestCase, override_settings
from django.urls import reverse
from djmoney.money import Money

from apps.reports.constants import (
    SUPPORTED_DOMAINS,
    ReportDomain,
    ReportGenerationStatus,
    ReportVisibility,
)
from apps.reports.datasets import (
    DatasetAccess,
    DatasetRelation,
    all_datasets,
    datasets_access_for_user,
    get_dataset,
)
from apps.reports.models import Report
from apps.reports.services.classification import (
    ReportClassification,
    build_classifier,
)
from apps.reports.services.compiler import (
    ReportCompiler,
    ReportPermissionDenied,
    chart_series,
)
from apps.reports.services.claude_classifier import (
    ClaudeClassifierError,
    ClaudeReportClassifier,
)
from apps.reports.services.claude_client import ClaudeAPIKeyMissing
from apps.reports.services.claude_spec import (
    ClaudeReportSpecGenerator,
    ClaudeSpecError,
)
from apps.reports.services.create_report import (
    ReportCreationError,
    ReportCreationService,
)
from apps.reports.tasks import generate_report
from apps.reports.services.entity_resolver import best_name_match, resolve_spec_names
from apps.reports.services.claude_suggestions import ClaudeReportPromptSuggester
from apps.reports.services.spec_generation import SpecGeneration
from apps.reports.services.summary import describe_spec
from apps.reports.spec import (
    ReportSpec,
    ReportSpecError,
    prefer_count_for_grouped_chart,
)
from accounting.models import (
    CompanyAccount,
    CostCenter,
    Journal,
    JournalEntry,
    JournalEntryLine,
    JournalType,
)
from assets.models import (
    Asset,
    AssetTransaction,
    AssetType,
    AssetTypeCategory,
    TransactionTypeChoices,
)
from banking.models import BankAccount, BankTransaction
from company.active_company import ActiveCompany
from company.models import BankAccount as CompanyBankAccount
from company.models import Company, CompanyUser, Employee
from configurator.models import Configuration, ConfigurationStatus, ProductTemplate
from finance.models import TaxPeriod, TaxScheduleRule
from inventory.choices import ItemType, MovementType
from inventory.models import InventoryItem, ItemTransaction, Warehouse
from invoices.models import (
    ConfirmationStatus,
    Invoice,
    InvoicePaymentStatus,
    SalesInvoice,
    SalesInvoiceStatus,
    TaxType,
)
from apps.einvoicing.models import (
    EInvoicingAccount,
    EInvoicingSubmission,
    SubmissionAction,
    SubmissionStatus,
)
from apps.expenses.models import ExpenseClaim, ExpenseClaimStatus
from feedback.models import FeedbackQuestionnaire, FeedbackResponse
from loans.models import Bank, Loan, LoanInstallment, LoanInstallmentPaymentStatus
from management.models import (
    CostEntry,
    ProjectFile,
    ProjectFileStatus,
    WorkEntry,
    WorkEntryDirection,
    WorkEntryType,
)
from production.models import (
    ProductionItem,
    ProductionItemSession,
    ProductionItemSessionStatus,
    ProductionTicket,
)
from materials.models import Category, Material, Provider
from payments.models import (
    Payment,
    PaymentAllocation,
    PaymentDirection,
    PaymentMethod,
    PaymentStatus,
)
from payroll.models import EmployeePayroll, PayrollPeriod
from purchasing.models import (
    PurchaseOrder,
    PurchaseOrderItem,
    PurchaseOrderStatus,
    PurchaseOrderType,
)
from sales.models import Client, Quote, QuoteStatus
from vacations.models import AbsenceReason, VacationRequest, VacationRequestStatus

User = get_user_model()


def _invoice_spec(**overrides):
    data = {
        "dataset": ReportDomain.SALES_INVOICES,
        "dimensions": ["status"],
        "aggregations": [
            {"measure": "*", "function": "count", "alias": "invoice_count"},
            {"measure": "total_amount", "function": "sum", "alias": "total"},
        ],
        "filters": [
            {
                "field": "date_issued",
                "op": "gte",
                "value": "2020-01-01",
                "parameter": "date_from",
            }
        ],
        "parameters": ["date_from"],
        "chart": "bar",
        "chart_label_dimension": "status",
        "chart_value_aggregation": "total",
    }
    data.update(overrides)
    return ReportSpec.from_dict(data)


def _dataset_user(company, *perms, username, **user_kwargs):
    """A staff user active in ``company`` holding ``perms`` ("app_label.codename")."""
    user_kwargs.setdefault("is_staff", True)
    user = User.objects.create_user(username=username, password="pass", **user_kwargs)
    if company is not None:
        CompanyUser.objects.create(user=user, company=company, is_active=True)
    for perm in perms:
        app_label, codename = perm.split(".")
        user.user_permissions.add(
            Permission.objects.get(
                content_type__app_label=app_label, codename=codename
            )
        )
    return user


def _agg(measure, function, alias):
    return {"measure": measure, "function": function, "alias": alias}


def _spec(dataset, dimensions=(), aggregations=(), **overrides):
    """A chartless, unfiltered spec on ``dataset``; any key can be overridden."""
    data = {
        "dataset": dataset,
        "dimensions": list(dimensions),
        "aggregations": list(aggregations),
        "filters": [],
        "parameters": [],
        "chart": "none",
        "chart_label_dimension": None,
        "chart_value_aggregation": None,
    }
    data.update(overrides)
    return ReportSpec.from_dict(data)


def _classifier(domain, confidence=0.95, needs_clarification=False, source="openai"):
    classifier = MagicMock()
    classifier.classify.return_value = ReportClassification(
        domain=domain,
        confidence=confidence,
        needs_clarification=needs_clarification,
        source=source,
    )
    return classifier


class ReportTestCase(TestCase):
    """Resets the active company after each test.

    ``spec_defaults`` holds the ``_spec`` arguments (dataset, dimensions,
    aggregations) a dataset class builds most of its reports from;
    ``self._spec(**overrides)`` layers per-test changes on top.
    """

    spec_defaults = {}

    def tearDown(self):
        ActiveCompany.set_current_ids(None)
        super().tearDown()

    def _spec(self, **overrides):
        return _spec(**{**self.spec_defaults, **overrides})


class GroupedChartMeasureTests(TestCase):
    def test_breakdown_without_money_charts_a_count_and_keeps_the_sum(self):
        spec = prefer_count_for_grouped_chart(
            _invoice_spec(),
            "Presupuestos aceptados frente a rechazados este año",
        )
        self.assertEqual(spec.chart_value_aggregation, "invoice_count")
        self.assertIn("total", {agg.alias for agg in spec.aggregations})

    def test_money_prompt_keeps_the_sum_on_the_chart(self):
        spec = prefer_count_for_grouped_chart(
            _invoice_spec(),
            "Total facturado este año por mes",
        )
        self.assertEqual(spec.chart_value_aggregation, "total")

    def test_hours_prompt_keeps_the_duration_on_the_chart(self):
        spec = prefer_count_for_grouped_chart(
            _invoice_spec(chart_value_aggregation="total"),
            "Horas de producción por etapa el mes pasado",
        )
        self.assertEqual(spec.chart_value_aggregation, "total")

    def test_worth_does_not_match_inside_another_word(self):
        spec = prefer_count_for_grouped_chart(
            _invoice_spec(),
            "Informe equivalente por estado",
        )
        self.assertEqual(spec.chart_value_aggregation, "invoice_count")


class TopChartTests(TestCase):
    def test_a_grouping_without_a_chart_uses_the_top_categories(self):
        rows = [{"client": f"C{index}", "invoice_count": index} for index in range(20)]
        spec = _invoice_spec(
            dimensions=["client"],
            chart="none",
            chart_label_dimension=None,
            chart_value_aggregation=None,
            filters=[],
            parameters=[],
        )
        labels, _values, chart_type, is_top, _urls = chart_series(spec, rows)
        self.assertEqual(chart_type, "bar")
        self.assertTrue(is_top)
        self.assertEqual(len(labels), 15)
        self.assertEqual(labels[0], "C19")

    def test_an_explicit_pie_chart_keeps_its_type(self):
        spec = _invoice_spec(
            dimensions=["client"],
            chart="pie",
            chart_label_dimension="client",
            chart_value_aggregation="invoice_count",
        )
        labels, _values, chart_type, is_top, _urls = chart_series(
            spec,
            [
                {"client": "A", "invoice_count": 2},
                {"client": "B", "invoice_count": 1},
            ],
        )
        self.assertEqual(chart_type, "pie")
        self.assertEqual(labels, ["A", "B"])
        self.assertFalse(is_top)

    def test_a_month_grouping_without_a_chart_is_a_line(self):
        spec = _invoice_spec(
            dimensions=["date_issued_month"],
            chart="none",
            chart_label_dimension=None,
            chart_value_aggregation=None,
            filters=[],
            parameters=[],
        )
        labels, _values, chart_type, _top, _urls = chart_series(
            spec,
            [{"date_issued_month": "2025-10-01", "invoice_count": 2}],
        )
        self.assertEqual(chart_type, "line")
        self.assertEqual(labels, ["2025-10-01"])

    def test_an_explicit_bar_on_a_month_grouping_stays_a_bar(self):
        spec = _invoice_spec(
            dimensions=["date_issued_month"],
            chart="bar",
            chart_label_dimension="date_issued_month",
            chart_value_aggregation="invoice_count",
            filters=[],
            parameters=[],
        )
        _labels, _values, chart_type, _top, _urls = chart_series(
            spec,
            [{"date_issued_month": "2025-10-01", "invoice_count": 2}],
        )
        self.assertEqual(chart_type, "bar")

    def test_a_listing_stays_without_a_chart(self):
        spec = _invoice_spec(
            dimensions=["invoice_number"],
            chart="none",
            chart_label_dimension=None,
            chart_value_aggregation=None,
            filters=[],
            parameters=[],
        )
        labels, _values, chart_type, is_top, _urls = chart_series(
            spec, [{"invoice_number": "1", "invoice_count": 1}]
        )
        self.assertEqual(labels, [])
        self.assertEqual(chart_type, "none")
        self.assertFalse(is_top)


class ReportSpecParserTests(TestCase):
    def test_rejects_unknown_aggregation(self):
        with self.assertRaises(ReportSpecError):
            ReportSpec.from_dict(
                {
                    "dataset": ReportDomain.SALES_INVOICES,
                    "dimensions": [],
                    "aggregations": [
                        {"measure": "*", "function": "median", "alias": "x"}
                    ],
                    "filters": [],
                    "parameters": [],
                    "chart": "none",
                    "chart_label_dimension": None,
                    "chart_value_aggregation": None,
                }
            )

    def test_rejects_unknown_dataset(self):
        with self.assertRaises(ReportSpecError):
            ReportSpec.from_dict(
                {
                    "dataset": "not_a_real_dataset",
                    "dimensions": [],
                    "aggregations": [
                        {"measure": "*", "function": "count", "alias": "n"}
                    ],
                    "filters": [],
                    "parameters": [],
                    "chart": "none",
                    "chart_label_dimension": None,
                    "chart_value_aggregation": None,
                }
            )

    def test_sanitizes_alias_with_spaces_and_accents(self):
        spec = ReportSpec.from_dict(
            {
                "dataset": ReportDomain.SALES_INVOICES,
                "dimensions": ["status"],
                "aggregations": [
                    {
                        "measure": "*",
                        "function": "count",
                        "alias": "Cantidad de facturas",
                    }
                ],
                "filters": [],
                "parameters": [],
                "chart": "bar",
                "chart_label_dimension": "status",
                "chart_value_aggregation": "Cantidad de facturas",
            }
        )
        self.assertEqual(spec.aggregations[0].alias, "Cantidad_de_facturas")
        self.assertEqual(spec.chart_value_aggregation, "Cantidad_de_facturas")

    def test_drops_unbound_parameters(self):
        spec = ReportSpec.from_dict(
            {
                "dataset": ReportDomain.SALES_INVOICES,
                "dimensions": ["client"],
                "aggregations": [
                    {"measure": "*", "function": "count", "alias": "n"}
                ],
                "filters": [
                    {
                        "field": "payment_status",
                        "op": "in",
                        "value": "unpaid,partial",
                        "parameter": None,
                    }
                ],
                "parameters": ["date_from", "date_to"],
                "chart": "none",
                "chart_label_dimension": None,
                "chart_value_aggregation": None,
            }
        )
        self.assertEqual(spec.parameters, [])


class ReportCompilerTests(ReportTestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company_a = Company.objects.create(name="Alpha Co", tax_identifier="A1")
        cls.company_b = Company.objects.create(name="Beta Co", tax_identifier="B1")
        cls.user = _dataset_user(
            cls.company_a, "invoices.view_salesinvoice", username="reporter"
        )

        client_a = Client.objects.create(company=cls.company_a, name="Client A")
        client_b = Client.objects.create(company=cls.company_b, name="Client B")
        SalesInvoice.objects.create(
            company=cls.company_a,
            client=client_a,
            invoice_number="A-1",
            total_amount=Money("100.00", "EUR"),
            date_issued=date(2024, 1, 15),
            status=SalesInvoiceStatus.FINAL,
        )
        SalesInvoice.objects.create(
            company=cls.company_b,
            client=client_b,
            invoice_number="B-1",
            total_amount=Money("999.00", "EUR"),
            date_issued=date(2024, 1, 15),
            status=SalesInvoiceStatus.FINAL,
        )

    def test_compiler_rejects_unknown_dimension(self):
        spec = _invoice_spec(
            dimensions=["not_a_field"],
            chart="none",
            chart_label_dimension=None,
            chart_value_aggregation=None,
        )
        with self.assertRaises(ReportSpecError):
            ReportCompiler().compile(spec, self.user)

    def test_groups_invoices_by_payment_method(self):
        ActiveCompany.set_current_ids([self.company_a.pk])
        spec = _invoice_spec(
            dimensions=["payment_method"],
            chart_label_dimension="payment_method",
            filters=[],
            parameters=[],
        )
        result = ReportCompiler().compile(spec, self.user)
        counts = {
            row["payment_method"]: row["invoice_count"] for row in result.rows
        }
        self.assertEqual(counts.get(PaymentMethod.TRANSFER), 1)

    def test_compiler_scopes_to_active_company(self):
        ActiveCompany.set_current_ids([self.company_a.pk])
        result = ReportCompiler().compile(_invoice_spec(), self.user)
        totals = {row["status"]: row["total"] for row in result.rows}
        self.assertEqual(totals.get(SalesInvoiceStatus.FINAL), 100.0)

    def test_permission_refusal_without_view_salesinvoice(self):
        bare = _dataset_user(self.company_a, username="noperm", is_staff=False)
        ActiveCompany.set_current_ids([self.company_a.pk])
        with self.assertRaises(ReportPermissionDenied):
            ReportCompiler().compile(_invoice_spec(), bare)

    def test_amount_threshold_filter(self):
        ActiveCompany.set_current_ids([self.company_a.pk])
        client = Client.objects.get(company=self.company_a, name="Client A")
        SalesInvoice.objects.create(
            company=self.company_a,
            client=client,
            invoice_number="A-BIG",
            total_amount=Money("15000.00", "EUR"),
            date_issued=date(2024, 2, 1),
            status=SalesInvoiceStatus.FINAL,
        )
        spec = _invoice_spec(
            dimensions=["invoice_number"],
            filters=[
                {
                    "field": "total_amount",
                    "op": "gt",
                    "value": "10000",
                    "parameter": None,
                }
            ],
            parameters=[],
            chart="none",
            chart_label_dimension=None,
            chart_value_aggregation=None,
        )
        result = ReportCompiler().compile(spec, self.user)
        numbers = {row["invoice_number"] for row in result.rows}
        self.assertEqual(numbers, {"A-BIG"})

    def test_client_name_filter_does_not_expect_id(self):
        ActiveCompany.set_current_ids([self.company_a.pk])
        spec = _invoice_spec(
            filters=[
                {
                    "field": "client",
                    "op": "eq",
                    "value": "Client A",
                    "parameter": None,
                }
            ],
            parameters=[],
        )
        result = ReportCompiler().compile(spec, self.user)
        self.assertEqual(len(result.rows), 1)
        self.assertEqual(result.rows[0]["total"], 100.0)

    def test_row_links_only_when_dimensions_identify_one_record(self):
        ActiveCompany.set_current_ids([self.company_a.pk])
        client = Client.objects.get(company=self.company_a, name="Client A")
        second_invoice = SalesInvoice.objects.create(
            company=self.company_a,
            client=client,
            invoice_number="A-2",
            total_amount=Money("50.00", "EUR"),
            date_issued=date(2024, 3, 1),
            status=SalesInvoiceStatus.FINAL,
        )
        first_invoice = SalesInvoice.objects.get(
            company=self.company_a, invoice_number="A-1"
        )

        listing_spec = _invoice_spec(
            dimensions=["invoice_number"],
            aggregations=[{"measure": "*", "function": "count", "alias": "n"}],
            filters=[],
            parameters=[],
            chart="none",
            chart_label_dimension=None,
            chart_value_aggregation=None,
        )
        result = ReportCompiler().compile(listing_spec, self.user)
        pks_by_invoice = dict(
            zip((row["invoice_number"] for row in result.rows), result.row_pks)
        )
        self.assertEqual(pks_by_invoice["A-1"], first_invoice.pk)
        self.assertEqual(pks_by_invoice["A-2"], second_invoice.pk)

        grouped_spec = _invoice_spec(
            dimensions=["status"],
            aggregations=[{"measure": "*", "function": "count", "alias": "n"}],
            filters=[],
            parameters=[],
            chart="none",
            chart_label_dimension=None,
            chart_value_aggregation=None,
        )
        grouped_result = ReportCompiler().compile(grouped_spec, self.user)
        self.assertTrue(all(pk is None for pk in grouped_result.row_pks))

    def test_entity_groups_link_and_months_do_not(self):
        ActiveCompany.set_current_ids([self.company_a.pk])
        client = Client.objects.get(company=self.company_a, name="Client A")
        SalesInvoice.objects.create(
            company=self.company_a,
            client=client,
            invoice_number="A-2",
            total_amount=Money("40.00", "EUR"),
            date_issued=date(2024, 2, 1),
            status=SalesInvoiceStatus.FINAL,
        )
        by_client = ReportCompiler().compile(
            _invoice_spec(
                dimensions=["client"],
                chart="bar",
                chart_label_dimension="client",
                chart_value_aggregation="invoice_count",
                filters=[],
                parameters=[],
            ),
            self.user,
        )
        client_url = reverse("admin:sales_client_change", args=[client.pk])
        self.assertEqual(by_client.row_pks, [None])
        self.assertEqual(by_client.chart_urls, [client_url])
        self.assertEqual(by_client.cell_urls[0][0], client_url)

        by_company = ReportCompiler().compile(
            _invoice_spec(
                dimensions=["company"],
                chart="bar",
                chart_label_dimension="company",
                chart_value_aggregation="invoice_count",
                filters=[],
                parameters=[],
            ),
            self.user,
        )
        company_url = reverse(
            "admin:company_company_change", args=[self.company_a.pk]
        )
        self.assertEqual(by_company.chart_urls, [company_url])

        by_month = ReportCompiler().compile(
            _invoice_spec(
                dimensions=["date_issued_month"],
                chart="line",
                chart_label_dimension="date_issued_month",
                chart_value_aggregation="invoice_count",
                filters=[],
                parameters=[],
            ),
            self.user,
        )
        self.assertTrue(by_month.chart_labels)
        self.assertTrue(all(url is None for url in by_month.chart_urls))
        self.assertTrue(all(url is None for url in by_month.row_urls))

        invoice = SalesInvoice.objects.get(
            company=self.company_a, invoice_number="A-1"
        )
        listing = ReportCompiler().compile(
            _invoice_spec(
                dimensions=["invoice_number"],
                aggregations=[{"measure": "*", "function": "count", "alias": "n"}],
                filters=[],
                parameters=[],
                chart="none",
                chart_label_dimension=None,
                chart_value_aggregation=None,
            ),
            self.user,
        )
        invoice_url = reverse(
            "admin:invoices_salesinvoice_change", args=[invoice.pk]
        )
        linked = {
            row["invoice_number"]: urls[0]
            for row, urls in zip(listing.rows, listing.cell_urls)
        }
        self.assertEqual(linked["A-1"], invoice_url)
        self.assertEqual(listing.chart_urls, [])

    def test_no_dimensions_means_no_row_link(self):
        ActiveCompany.set_current_ids([self.company_a.pk])
        spec = _invoice_spec(
            dimensions=[],
            filters=[],
            parameters=[],
            chart="none",
            chart_label_dimension=None,
            chart_value_aggregation=None,
        )
        result = ReportCompiler().compile(spec, self.user)
        self.assertEqual(result.row_pks, [None])


class ClientsDatasetAndRelationsTests(TestCase):
    """The clients dataset fills the gap where Client had no first-class
    dataset of its own (only a filter on other datasets), and relation
    metadata lets a caller navigate client -> project files/quotes/
    invoices/payments without hardcoding Django relations outside the
    catalog."""

    @classmethod
    def setUpTestData(cls):
        cls.company_a = Company.objects.create(name="Alpha Co", tax_identifier="A1")
        cls.company_b = Company.objects.create(name="Beta Co", tax_identifier="B1")
        cls.user = _dataset_user(
            cls.company_a, "sales.view_client", username="client-reporter"
        )

        cls.client_a1 = Client.objects.create(
            company=cls.company_a, name="Client A1", is_insured=True
        )
        cls.client_a2 = Client.objects.create(
            company=cls.company_a, name="Client A2", is_insured=False
        )
        cls.client_b1 = Client.objects.create(company=cls.company_b, name="Client B1")

    def test_clients_is_a_supported_domain(self):
        self.assertIn(ReportDomain.CLIENTS, SUPPORTED_DOMAINS)
        self.assertIn(
            ReportDomain.CLIENTS,
            {access.key for access in datasets_access_for_user(self.user)},
        )

    def test_compiles_a_grouped_report_scoped_to_company(self):
        spec = ReportSpec.from_dict(
            {
                "dataset": ReportDomain.CLIENTS,
                "dimensions": ["is_insured"],
                "aggregations": [
                    {"measure": "*", "function": "count", "alias": "client_count"}
                ],
                "filters": [],
                "parameters": [],
                "chart": "none",
                "chart_label_dimension": None,
                "chart_value_aggregation": None,
            }
        )
        result = ReportCompiler().compile(spec, self.user)
        totals = {row["is_insured"]: row["client_count"] for row in result.rows}
        self.assertEqual(totals, {True: 1, False: 1})

    def test_get_by_id_returns_row_within_company_scope(self):
        row = ReportCompiler().get_by_id(
            ReportDomain.CLIENTS, self.client_a1.pk, self.user
        )
        self.assertEqual(row["name"], "Client A1")
        self.assertTrue(row["is_insured"])

    def test_get_by_id_returns_none_outside_company_scope(self):
        row = ReportCompiler().get_by_id(
            ReportDomain.CLIENTS, self.client_b1.pk, self.user
        )
        self.assertIsNone(row)

    def test_get_by_id_denies_without_permission(self):
        other_user = _dataset_user(self.company_a, username="no-perm")
        with self.assertRaises(ReportPermissionDenied):
            ReportCompiler().get_by_id(
                ReportDomain.CLIENTS, self.client_a1.pk, other_user
            )

    def test_project_files_relation_points_to_clients_and_back(self):
        clients_relations = get_dataset(ReportDomain.CLIENTS).relation_map()
        self.assertIn("project_files", clients_relations)
        self.assertEqual(
            clients_relations["project_files"].target_dataset,
            ReportDomain.PROJECT_FILES,
        )

        project_files_relations = get_dataset(
            ReportDomain.PROJECT_FILES
        ).relation_map()
        self.assertIn("client", project_files_relations)
        relation = project_files_relations["client"]
        self.assertIsInstance(relation, DatasetRelation)
        self.assertEqual(relation.target_dataset, ReportDomain.CLIENTS)
        self.assertEqual(relation.kind, "to_one")


class RelationTraversalTests(TestCase):
    """traverse_relation() must follow the catalog's declared relations in
    both directions using only each dataset's own base_queryset()."""

    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name="Traverse Co", tax_identifier="T1")
        cls.user = _dataset_user(
            cls.company,
            "sales.view_client",
            "management.view_projectfile",
            "sales.view_quote",
            "invoices.view_salesinvoice",
            username="traverser",
        )

        cls.client_obj = Client.objects.create(
            company=cls.company, name="Traverse Client"
        )
        cls.project_file = ProjectFile.objects.create(
            company=cls.company, name="EXP Traverse", client=cls.client_obj
        )
        cls.quote = Quote.objects.create(
            company=cls.company,
            client=cls.client_obj,
            project_file=cls.project_file,
            total_price=Money("500.00", "EUR"),
        )
        cls.invoice = SalesInvoice.objects.create(
            company=cls.company,
            client=cls.client_obj,
            project_file=cls.project_file,
            invoice_number="TR-1",
            total_amount=Money("500.00", "EUR"),
            date_issued=date(2026, 1, 15),
            status=SalesInvoiceStatus.FINAL,
        )

    def test_to_many_from_client_to_project_files(self):
        rows = ReportCompiler().traverse_relation(
            ReportDomain.CLIENTS, self.client_obj.pk, "project_files", self.user
        )
        self.assertEqual([r["id"] for r in rows], [self.project_file.pk])

    def test_to_many_from_project_file_to_quotes(self):
        rows = ReportCompiler().traverse_relation(
            ReportDomain.PROJECT_FILES, self.project_file.pk, "quotes", self.user
        )
        self.assertEqual([r["id"] for r in rows], [self.quote.pk])

    def test_to_many_from_project_file_to_sales_invoices(self):
        rows = ReportCompiler().traverse_relation(
            ReportDomain.PROJECT_FILES,
            self.project_file.pk,
            "sales_invoices",
            self.user,
        )
        self.assertEqual([r["id"] for r in rows], [self.invoice.pk])

    def test_to_one_from_invoice_to_client(self):
        rows = ReportCompiler().traverse_relation(
            ReportDomain.SALES_INVOICES, self.invoice.pk, "client", self.user
        )
        self.assertEqual([r["id"] for r in rows], [self.client_obj.pk])

    def test_to_one_from_quote_to_project_file(self):
        rows = ReportCompiler().traverse_relation(
            ReportDomain.QUOTES, self.quote.pk, "project_file", self.user
        )
        self.assertEqual([r["id"] for r in rows], [self.project_file.pk])

    def test_unknown_relation_raises(self):
        with self.assertRaises(ReportSpecError):
            ReportCompiler().traverse_relation(
                ReportDomain.CLIENTS, self.client_obj.pk, "not_a_relation", self.user
            )

    def test_denies_without_target_permission(self):
        limited_user = _dataset_user(
            self.company, "sales.view_client", username="limited-traverser"
        )
        with self.assertRaises(ReportPermissionDenied):
            ReportCompiler().traverse_relation(
                ReportDomain.CLIENTS,
                self.client_obj.pk,
                "project_files",
                limited_user,
            )


class SalesInvoiceAmountRemainingTests(ReportTestCase):
    """amount_remaining must reflect payments applied, not just total_amount."""

    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name="Debt Co", tax_identifier="DEBT1")
        cls.user = _dataset_user(
            cls.company, "invoices.view_salesinvoice", username="debtreporter"
        )

        cls.client_obj = Client.objects.create(company=cls.company, name="Debtor")
        cls.unpaid_invoice = SalesInvoice.objects.create(
            company=cls.company,
            client=cls.client_obj,
            invoice_number="DEBT-1",
            total_amount=Money("300.00", "EUR"),
            date_issued=date(2024, 1, 1),
            status=SalesInvoiceStatus.FINAL,
        )
        cls.partially_paid_invoice = SalesInvoice.objects.create(
            company=cls.company,
            client=cls.client_obj,
            invoice_number="DEBT-2",
            total_amount=Money("200.00", "EUR"),
            date_issued=date(2024, 1, 2),
            status=SalesInvoiceStatus.FINAL,
        )
        payment = Payment.objects.create(
            company=cls.company,
            amount=Money("120.00", "EUR"),
            payment_date=date(2024, 1, 10),
            direction=PaymentDirection.INBOUND,
            method=PaymentMethod.TRANSFER,
            status=PaymentStatus.CONFIRMED,
        )
        PaymentAllocation.objects.create(
            payment=payment,
            related_object=cls.partially_paid_invoice,
            amount_applied=Money("120.00", "EUR"),
        )

    def test_amount_remaining_subtracts_applied_payments(self):
        ActiveCompany.set_current_ids([self.company.pk])
        spec = ReportSpec.from_dict(
            {
                "dataset": ReportDomain.SALES_INVOICES,
                "dimensions": ["invoice_number"],
                "aggregations": [
                    {
                        "measure": "amount_remaining",
                        "function": "sum",
                        "alias": "remaining",
                    }
                ],
                "filters": [],
                "parameters": [],
                "chart": "none",
                "chart_label_dimension": None,
                "chart_value_aggregation": None,
            }
        )
        result = ReportCompiler().compile(spec, self.user)
        remaining_by_number = {
            row["invoice_number"]: row["remaining"] for row in result.rows
        }
        self.assertEqual(remaining_by_number["DEBT-1"], 300.0)
        self.assertEqual(remaining_by_number["DEBT-2"], 80.0)

    def test_amount_remaining_groups_correctly_by_client(self):
        ActiveCompany.set_current_ids([self.company.pk])
        spec = ReportSpec.from_dict(
            {
                "dataset": ReportDomain.SALES_INVOICES,
                "dimensions": ["client"],
                "aggregations": [
                    {
                        "measure": "amount_remaining",
                        "function": "sum",
                        "alias": "remaining",
                    }
                ],
                "filters": [],
                "parameters": [],
                "chart": "none",
                "chart_label_dimension": None,
                "chart_value_aggregation": None,
            }
        )
        result = ReportCompiler().compile(spec, self.user)
        self.assertEqual(len(result.rows), 1)
        self.assertEqual(result.rows[0]["remaining"], 380.0)


class InventoryDatasetTests(ReportTestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name="Inv Co", tax_identifier="INV1")
        cls.user = _dataset_user(
            cls.company, "inventory.view_inventoryitem", username="invreporter"
        )

        cls.warehouse = Warehouse.objects.create(
            company=cls.company, code="WH1", name="Main Warehouse"
        )
        cls.item = InventoryItem.objects.create(
            company=cls.company,
            name="Aluminium Profile 40x40",
            item_type=ItemType.STOCKABLE,
            default_warehouse=cls.warehouse,
            min_stock=10,
            cost_price=Money("2.50", "EUR"),
        )
        ItemTransaction.objects.create(
            item=cls.item,
            warehouse=cls.warehouse,
            movement_type=MovementType.IN,
            quantity_change=50,
        )
        ItemTransaction.objects.create(
            item=cls.item,
            warehouse=cls.warehouse,
            movement_type=MovementType.OUT,
            quantity_change=-20,
        )

    spec_defaults = {
        "dataset": ReportDomain.INVENTORY,
        "dimensions": ["code"],
        "aggregations": [_agg("*", "count", "n")],
    }

    def test_stock_on_hand_computed_via_report(self):
        ActiveCompany.set_current_ids([self.company.pk])
        spec = self._spec(
            dimensions=["code", "name"],
            aggregations=[
                {
                    "measure": "stock_on_hand",
                    "function": "sum",
                    "alias": "stock_on_hand",
                }
            ],
        )
        result = ReportCompiler().compile(spec, self.user)
        self.assertEqual(len(result.rows), 1)
        self.assertEqual(result.rows[0]["stock_on_hand"], 30.0)
        self.assertEqual(result.row_pks, [self.item.pk])

    def test_low_stock_filter_excludes_items_above_threshold(self):
        ActiveCompany.set_current_ids([self.company.pk])
        spec = self._spec(
            filters=[
                {"field": "stock_on_hand", "op": "lt", "value": "10", "parameter": None}
            ],
        )
        result = ReportCompiler().compile(spec, self.user)
        self.assertEqual(result.rows, [])

    def test_low_stock_uses_item_minimum_and_skips_composites(self):
        ActiveCompany.set_current_ids([self.company.pk])
        low = InventoryItem.objects.create(
            company=self.company,
            name="Short bar",
            item_type=ItemType.STOCKABLE,
            default_warehouse=self.warehouse,
            min_stock=40,
            cost_price=Money("1.00", "EUR"),
        )
        ItemTransaction.objects.create(
            item=low,
            warehouse=self.warehouse,
            movement_type=MovementType.IN,
            quantity_change=5,
        )
        composite = InventoryItem.objects.create(
            company=self.company,
            name="Assembled frame",
            item_type=ItemType.STOCKABLE,
            default_warehouse=self.warehouse,
            min_stock=1,
            has_components=True,
            cost_price=Money("1.00", "EUR"),
        )
        spec = self._spec(
            dimensions=["code"],
            filters=[
                {
                    "field": "is_low_stock",
                    "op": "eq",
                    "value": "true",
                    "parameter": None,
                }
            ],
        )
        result = ReportCompiler().compile(spec, self.user)
        codes = {row["code"] for row in result.rows}
        self.assertIn(low.code, codes)
        self.assertNotIn(self.item.code, codes)
        self.assertNotIn(composite.code, codes)

    def test_stock_value_is_stock_on_hand_times_cost_price(self):
        # stock_on_hand=30 (50 in - 20 out), cost_price=2.50 -> 75.00.
        ActiveCompany.set_current_ids([self.company.pk])
        spec = self._spec(
            dimensions=["code"],
            aggregations=[
                {"measure": "stock_value", "function": "sum", "alias": "stock_value"}
            ],
        )
        result = ReportCompiler().compile(spec, self.user)
        self.assertEqual(len(result.rows), 1)
        self.assertEqual(result.rows[0]["stock_value"], 75.0)

    def test_stock_value_filter_threshold(self):
        ActiveCompany.set_current_ids([self.company.pk])
        spec = self._spec(
            filters=[
                {"field": "stock_value", "op": "gt", "value": "100", "parameter": None}
            ],
        )
        result = ReportCompiler().compile(spec, self.user)
        self.assertEqual(result.rows, [])  # 75.00 is not > 100

        spec_matching = self._spec(
            filters=[
                {"field": "stock_value", "op": "gt", "value": "50", "parameter": None}
            ],
        )
        result_matching = ReportCompiler().compile(spec_matching, self.user)
        self.assertEqual(len(result_matching.rows), 1)


class QuotesDatasetInvoicedFilterTests(ReportTestCase):
    """A quote is 'invoiced' only via its project file's cached total."""

    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name="Quote Co", tax_identifier="Q1")
        cls.user = _dataset_user(
            cls.company, "sales.view_quote", username="quotereporter"
        )

        client = Client.objects.create(company=cls.company, name="Quote Client")

        cls.invoiced_project_file = ProjectFile.objects.create(
            company=cls.company,
            name="Invoiced expediente",
            client=client,
        )
        cls.uninvoiced_project_file = ProjectFile.objects.create(
            company=cls.company,
            name="Uninvoiced expediente",
            client=client,
        )
        cls.invoiced_quote = Quote.objects.create(
            company=cls.company,
            client=client,
            project_file=cls.invoiced_project_file,
            total_price=Money("500.00", "EUR"),
        )
        cls.uninvoiced_quote = Quote.objects.create(
            company=cls.company,
            client=client,
            project_file=cls.uninvoiced_project_file,
            total_price=Money("200.00", "EUR"),
        )
        cls.no_project_file_quote = Quote.objects.create(
            company=cls.company,
            client=client,
            total_price=Money("100.00", "EUR"),
        )
        # Saving a real SalesInvoice against the project file is what the
        # app itself uses to populate ProjectFile.total_invoiced (see
        # management.signals.sales_invoice_saved_update_project_file) —
        # setting the field directly would just get overwritten by it.
        SalesInvoice.objects.create(
            company=cls.company,
            client=client,
            project_file=cls.invoiced_project_file,
            invoice_number="Q-INV-1",
            subtotal_amount=Money("500.00", "EUR"),
        )

    spec_defaults = {
        "dataset": ReportDomain.QUOTES,
        "dimensions": ["client"],
        "aggregations": [_agg("*", "count", "n")],
    }

    def test_is_invoiced_false_excludes_quotes_with_invoiced_project_files(self):
        ActiveCompany.set_current_ids([self.company.pk])
        spec = self._spec(
            dimensions=[],
            filters=[
                {
                    "field": "is_invoiced",
                    "op": "eq",
                    "value": "false",
                    "parameter": None,
                }
            ],
            aggregations=[
                {"measure": "*", "function": "count", "alias": "n"}
            ],
        )
        result = ReportCompiler().compile(spec, self.user)
        self.assertEqual(result.rows[0]["n"], 2)

    def test_is_invoiced_true_only_matches_project_files_with_invoiced_amount(self):
        ActiveCompany.set_current_ids([self.company.pk])
        spec = self._spec(
            dimensions=[],
            filters=[
                {"field": "is_invoiced", "op": "eq", "value": "true", "parameter": None}
            ],
            aggregations=[
                {"measure": "*", "function": "count", "alias": "n"}
            ],
        )
        result = ReportCompiler().compile(spec, self.user)
        self.assertEqual(result.rows[0]["n"], 1)


class ProjectFilesDatasetTests(ReportTestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(
            name="Expediente Co", tax_identifier="EXP1"
        )
        cls.user = _dataset_user(
            cls.company, "management.view_projectfile", username="pfreporter"
        )

        client = Client.objects.create(company=cls.company, name="Expediente Client")

        cls.invoiced = ProjectFile.objects.create(
            company=cls.company,
            name="Fully invoiced",
            client=client,
            status=ProjectFileStatus.ACTIVE,
            total_amount=Money("1000.00", "EUR"),
            total_invoiced=Money("1000.00", "EUR"),
        )
        cls.pending = ProjectFile.objects.create(
            company=cls.company,
            name="Not invoiced yet",
            client=client,
            status=ProjectFileStatus.ACTIVE,
            total_amount=Money("800.00", "EUR"),
        )

    spec_defaults = {
        "dataset": ReportDomain.PROJECT_FILES,
        "dimensions": ["reference"],
        "aggregations": [_agg("*", "count", "n")],
    }

    def test_not_invoiced_filter_only_returns_pending_project_file(self):
        ActiveCompany.set_current_ids([self.company.pk])
        spec = self._spec(
            filters=[
                {
                    "field": "is_invoiced",
                    "op": "eq",
                    "value": "false",
                    "parameter": None,
                }
            ],
        )
        result = ReportCompiler().compile(spec, self.user)
        references = {row["reference"] for row in result.rows}
        self.assertEqual(references, {self.pending.reference})

    def test_total_amount_measure_sums_correctly(self):
        ActiveCompany.set_current_ids([self.company.pk])
        spec = self._spec(
            dimensions=[],
            aggregations=[
                {"measure": "total_amount", "function": "sum", "alias": "total"}
            ],
        )
        result = ReportCompiler().compile(spec, self.user)
        self.assertEqual(result.rows[0]["total"], 1800.0)


class PurchasingDatasetTests(ReportTestCase):
    """Accounts payable: what we owe providers, net of payments applied."""

    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name="AP Co", tax_identifier="AP1")
        cls.user = _dataset_user(
            cls.company, "invoices.view_invoice", username="apreporter"
        )

        cls.provider = Provider.objects.create(company=cls.company, name="Perfiles SA")
        cls.unpaid_invoice = Invoice.objects.create(
            company=cls.company,
            provider=cls.provider,
            invoice_number="PROV-1",
            date_issued=date(2024, 1, 1),
            total_amount=Money("500.00", "EUR"),
            payment_status=InvoicePaymentStatus.UNPAID,
        )
        cls.paid_invoice = Invoice.objects.create(
            company=cls.company,
            provider=cls.provider,
            invoice_number="PROV-2",
            date_issued=date(2024, 1, 2),
            total_amount=Money("300.00", "EUR"),
            payment_status=InvoicePaymentStatus.PAID,
        )
        payment = Payment.objects.create(
            company=cls.company,
            amount=Money("300.00", "EUR"),
            payment_date=date(2024, 1, 10),
            direction=PaymentDirection.OUTBOUND,
            method=PaymentMethod.TRANSFER,
            status=PaymentStatus.CONFIRMED,
        )
        PaymentAllocation.objects.create(
            payment=payment,
            related_object=cls.paid_invoice,
            amount_applied=Money("300.00", "EUR"),
        )

        cls.workshop = CostCenter.objects.create(
            company=cls.company, code="CC-1", name="Taller"
        )
        cls.unpaid_invoice.cost_center = cls.workshop
        cls.unpaid_invoice.save(update_fields=["cost_center"])

        # Confirmation-status fixtures live in their own company: invoices
        # default to PENDING, so sharing a company would leak PROV-* rows.
        cls.confirm_company = Company.objects.create(
            name="Confirm Co", tax_identifier="CFM1"
        )
        CompanyUser.objects.create(
            user=cls.user, company=cls.confirm_company, is_active=True
        )
        confirm_provider = Provider.objects.create(
            company=cls.confirm_company, name="Perfiles SA"
        )
        Invoice.objects.create(
            company=cls.confirm_company,
            provider=confirm_provider,
            invoice_number="CONF-1",
            date_issued=date(2024, 1, 1),
            total_amount=Money("100.00", "EUR"),
            confirmation_status=ConfirmationStatus.PENDING,
        )
        Invoice.objects.create(
            company=cls.confirm_company,
            provider=confirm_provider,
            invoice_number="CONF-2",
            date_issued=date(2024, 1, 2),
            total_amount=Money("200.00", "EUR"),
            confirmation_status=ConfirmationStatus.APPROVED,
        )

    spec_defaults = {
        "dataset": ReportDomain.PURCHASING,
        "dimensions": ["provider"],
        "aggregations": [_agg("amount_remaining", "sum", "owed")],
    }

    def test_cost_center_filter_and_dimension(self):
        ActiveCompany.set_current_ids([self.company.pk])
        spec = self._spec(
            dimensions=["cost_center"],
            aggregations=[{"measure": "*", "function": "count", "alias": "n"}],
            filters=[
                {
                    "field": "cost_center",
                    "op": "eq",
                    "value": "Taller",
                    "parameter": None,
                }
            ],
        )
        result = ReportCompiler().compile(spec, self.user)
        self.assertEqual(
            [row["cost_center"] for row in result.rows], ["Taller"]
        )

    def test_amount_remaining_only_counts_the_unpaid_invoice(self):
        ActiveCompany.set_current_ids([self.company.pk])
        result = ReportCompiler().compile(self._spec(), self.user)
        self.assertEqual(len(result.rows), 1)
        self.assertEqual(result.rows[0]["owed"], 500.0)

    def test_filters_to_pending_confirmation_only(self):
        ActiveCompany.set_current_ids([self.confirm_company.pk])
        spec = self._spec(
            dimensions=["invoice_number"],
            aggregations=[_agg("*", "count", "n")],
            filters=[
                {
                    "field": "confirmation_status",
                    "op": "eq",
                    "value": "pending",
                    "parameter": None,
                }
            ],
        )
        result = ReportCompiler().compile(spec, self.user)
        self.assertEqual(
            [row["invoice_number"] for row in result.rows], ["CONF-1"]
        )


class PurchaseOrdersDatasetTests(ReportTestCase):
    """Orders sent to providers: amount, quantities, billing and receipt."""

    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name="PO Co", tax_identifier="PO1")
        cls.other = Company.objects.create(name="Other Co", tax_identifier="PO2")
        cls.user = _dataset_user(
            cls.company, "purchasing.view_purchaseorder", username="poreporter"
        )
        cls.rejecter = User.objects.create_user(username="porejecter", password="pass")

        cls.warehouse = Warehouse.objects.create(
            company=cls.company, code="POWH", name="PO Warehouse"
        )
        cls.item = InventoryItem.objects.create(
            company=cls.company,
            name="Profile",
            item_type=ItemType.STOCKABLE,
            default_warehouse=cls.warehouse,
        )
        cls.provider = Provider.objects.create(
            company=cls.company, name="Perfiles SA", payment_terms_days=60
        )
        cls.center = CostCenter.objects.create(
            company=cls.company, code="PO-CC", name="Compras"
        )
        cls.client = Client.objects.create(company=cls.company, name="Obra Norte")
        cls.project = ProjectFile.objects.create(
            company=cls.company, client=cls.client, name="Nave"
        )

        cls.partial = PurchaseOrder.objects.create(
            company=cls.company,
            provider=cls.provider,
            po_type=PurchaseOrderType.STOCK,
            status=PurchaseOrderStatus.APPROVED,
            cost_center=cls.center,
            external_reference="ALB-9",
            date_ordered=date(2026, 3, 1),
            requested_by=cls.user,
        )
        first = PurchaseOrderItem.objects.create(
            purchase_order=cls.partial,
            inventory_item=cls.item,
            quantity=Decimal("2"),
            unit_price=Decimal("10"),
        )
        second = PurchaseOrderItem.objects.create(
            purchase_order=cls.partial,
            inventory_item=cls.item,
            quantity=Decimal("1"),
            unit_price=Decimal("10"),
        )
        first.received_quantity = Decimal("1")
        first.save(update_fields=["received_quantity"])
        second.received_quantity = Decimal("0")
        second.save(update_fields=["received_quantity"])

        cls.done = PurchaseOrder.objects.create(
            company=cls.company,
            provider=cls.provider,
            po_type=PurchaseOrderType.STOCK,
            status=PurchaseOrderStatus.CLOSED,
            date_ordered=date(2026, 4, 1),
        )
        cls.done.payment_terms_days = 15
        cls.done.save(update_fields=["payment_terms_days"])
        done_line = PurchaseOrderItem.objects.create(
            purchase_order=cls.done,
            inventory_item=cls.item,
            quantity=Decimal("4"),
            unit_price=Decimal("5"),
        )
        done_line.received_quantity = Decimal("4")
        done_line.invoiced_quantity = Decimal("4")
        done_line.save(update_fields=["received_quantity", "invoiced_quantity"])

        cls.rejected = PurchaseOrder.objects.create(
            company=cls.company,
            provider=cls.provider,
            po_type=PurchaseOrderType.PROJECT,
            project_file=cls.project,
            status=PurchaseOrderStatus.REJECTED,
            rejected_by=cls.rejecter,
            date_ordered=date(2026, 2, 1),
        )
        cls.rejected.payment_terms_days = 0
        cls.rejected.save(update_fields=["payment_terms_days"])

        other_provider = Provider.objects.create(company=cls.other, name="Ajena")
        PurchaseOrder.objects.create(
            company=cls.other,
            provider=other_provider,
            po_type=PurchaseOrderType.STOCK,
            status=PurchaseOrderStatus.APPROVED,
        )

    spec_defaults = {
        "dataset": ReportDomain.PURCHASE_ORDERS,
        "dimensions": ["provider"],
        "aggregations": [_agg("total_amount", "sum", "total")],
    }

    def test_total_is_not_multiplied_by_the_number_of_lines(self):
        ActiveCompany.set_current_ids([self.company.pk])
        spec = self._spec(
            dimensions=["number"],
            filters=[
                {
                    "field": "external_reference",
                    "op": "eq",
                    "value": "ALB-9",
                    "parameter": None,
                }
            ],
        )
        result = ReportCompiler().compile(spec, self.user)
        self.assertEqual(len(result.rows), 1)
        self.assertEqual(result.rows[0]["total"], 30.0)

    def test_quantities_and_progress_by_order(self):
        ActiveCompany.set_current_ids([self.company.pk])
        spec = self._spec(
            dimensions=["invoicing_status", "receiving_status"],
            aggregations=[
                {
                    "measure": "ordered_quantity",
                    "function": "sum",
                    "alias": "ordered",
                },
                {
                    "measure": "received_quantity",
                    "function": "sum",
                    "alias": "received",
                },
                {
                    "measure": "invoiced_quantity",
                    "function": "sum",
                    "alias": "invoiced",
                },
            ],
            filters=[
                {
                    "field": "status",
                    "op": "eq",
                    "value": PurchaseOrderStatus.APPROVED,
                    "parameter": None,
                }
            ],
        )
        result = ReportCompiler().compile(spec, self.user)
        self.assertEqual(len(result.rows), 1)
        row = result.rows[0]
        self.assertEqual(row["invoicing_status"], "not_invoiced")
        self.assertEqual(row["receiving_status"], "partially_received")
        self.assertEqual(row["ordered"], Decimal("3"))
        self.assertEqual(row["received"], Decimal("1"))
        self.assertEqual(row["invoiced"], Decimal("0"))

    def test_fully_received_and_invoiced_order(self):
        ActiveCompany.set_current_ids([self.company.pk])
        spec = self._spec(
            dimensions=["number"],
            aggregations=[{"measure": "*", "function": "count", "alias": "n"}],
            filters=[
                {
                    "field": "receiving_status",
                    "op": "eq",
                    "value": "fully_received",
                    "parameter": None,
                },
                {
                    "field": "invoicing_status",
                    "op": "eq",
                    "value": "fully_invoiced",
                    "parameter": None,
                },
            ],
        )
        result = ReportCompiler().compile(spec, self.user)
        self.assertEqual(len(result.rows), 1)
        self.assertEqual(result.rows[0]["number"], self.done.number)

    def test_payment_terms_and_rejecter(self):
        ActiveCompany.set_current_ids([self.company.pk])
        terms = self._spec(
            dimensions=["number"],
            aggregations=[{"measure": "*", "function": "count", "alias": "n"}],
            filters=[
                {
                    "field": "payment_terms_days",
                    "op": "gt",
                    "value": "30",
                    "parameter": None,
                }
            ],
        )
        result = ReportCompiler().compile(terms, self.user)
        self.assertEqual(
            [row["number"] for row in result.rows], [self.partial.number]
        )

        rejected = self._spec(
            dimensions=["rejected_by"],
            aggregations=[{"measure": "*", "function": "count", "alias": "n"}],
            filters=[
                {
                    "field": "status",
                    "op": "eq",
                    "value": PurchaseOrderStatus.REJECTED,
                    "parameter": None,
                }
            ],
        )
        result = ReportCompiler().compile(rejected, self.user)
        self.assertEqual(result.rows[0]["rejected_by"], "porejecter")

    def test_other_company_orders_are_excluded(self):
        ActiveCompany.set_current_ids([self.company.pk])
        spec = self._spec(
            dimensions=[],
            aggregations=[{"measure": "*", "function": "count", "alias": "n"}],
        )
        result = ReportCompiler().compile(spec, self.user)
        self.assertEqual(result.rows[0]["n"], 3)

    def test_listing_by_number_stays_a_table(self):
        spec = self._spec(dimensions=["number"], chart="none")
        _labels, _values, chart_type, _top, _urls = chart_series(spec, [])
        self.assertEqual(chart_type, "none")


class MultiCompanyFilterTests(ReportTestCase):
    """Naming multiple companies/clients must OR them, not pick one.

    Regression: two ANDed filters on the same field match nothing, and
    picking only the first name silently drops the rest.
    """

    @classmethod
    def setUpTestData(cls):
        cls.company_a = Company.objects.create(name="Aluares", tax_identifier="ALU1")
        cls.company_b = Company.objects.create(name="San Carlos", tax_identifier="SC1")
        cls.user = _dataset_user(
            cls.company_a, "invoices.view_salesinvoice", username="multico"
        )
        CompanyUser.objects.create(user=cls.user, company=cls.company_b, is_active=True)

        client_a = Client.objects.create(company=cls.company_a, name="Client A")
        client_b = Client.objects.create(company=cls.company_b, name="Client B")
        SalesInvoice.objects.create(
            company=cls.company_a,
            client=client_a,
            invoice_number="ALU-1",
            total_amount=Money("15000.00", "EUR"),
            date_issued=date(2026, 1, 1),
            status=SalesInvoiceStatus.FINAL,
        )
        SalesInvoice.objects.create(
            company=cls.company_b,
            client=client_b,
            invoice_number="SC-1",
            total_amount=Money("20000.00", "EUR"),
            date_issued=date(2026, 1, 1),
            status=SalesInvoiceStatus.FINAL,
        )

    def _spec(self, value):
        return _invoice_spec(
            dimensions=["invoice_number"],
            aggregations=[{"measure": "*", "function": "count", "alias": "n"}],
            filters=[
                {"field": "company", "op": "in", "value": value, "parameter": None}
            ],
            parameters=[],
            chart="none",
            chart_label_dimension=None,
            chart_value_aggregation=None,
        )

    def test_in_filter_matches_multiple_companies_via_icontains(self):
        ActiveCompany.set_current_ids([self.company_a.pk, self.company_b.pk])
        result = ReportCompiler().compile(self._spec("Aluares,San Carlos"), self.user)
        numbers = {row["invoice_number"] for row in result.rows}
        self.assertEqual(numbers, {"ALU-1", "SC-1"})

    def test_in_filter_does_not_require_exact_match(self):
        ActiveCompany.set_current_ids([self.company_a.pk, self.company_b.pk])
        result = ReportCompiler().compile(self._spec("Carlos"), self.user)
        numbers = {row["invoice_number"] for row in result.rows}
        self.assertEqual(numbers, {"SC-1"})

    def test_entity_resolver_resolves_each_name_in_list(self):
        spec = self._spec("aluares, san carlos")
        resolved, warnings = resolve_spec_names(spec, self.user)
        self.assertEqual(warnings, [])
        self.assertEqual(resolved.filters[0].op, "in")
        self.assertEqual(
            set(resolved.filters[0].value), {"Aluares", "San Carlos"}
        )

    def test_entity_resolver_drops_unmatched_name_but_keeps_others(self):
        spec = self._spec("aluares, nonexistent company")
        resolved, warnings = resolve_spec_names(spec, self.user)
        self.assertEqual(len(warnings), 1)
        self.assertIn("nonexistent company", warnings[0])
        self.assertEqual(resolved.filters[0].value, "Aluares")

    def test_client_names_with_commas_are_not_split_on_the_suffix(self):
        indice = Client.objects.create(
            company=self.company_a, name="INDICE FACILITY SERVICES, S.L."
        )
        micro = Client.objects.create(
            company=self.company_a, name="MICROCLIMA, S.A."
        )
        other = Client.objects.create(company=self.company_a, name="PROSILAC, S.A.")
        for client, number in (
            (indice, "IND-1"),
            (micro, "MIC-1"),
            (other, "PRO-1"),
        ):
            SalesInvoice.objects.create(
                company=self.company_a,
                client=client,
                invoice_number=number,
                total_amount=Money("10.00", "EUR"),
                date_issued=date(2026, 3, 1),
                status=SalesInvoiceStatus.FINAL,
            )
        ActiveCompany.set_current_ids([self.company_a.pk, self.company_b.pk])
        spec = _invoice_spec(
            dimensions=["client"],
            filters=[
                {
                    "field": "client",
                    "op": "in",
                    "value": "INDICE FACILITY SERVICES, S.L.,MICROCLIMA, S.A.",
                    "parameter": None,
                }
            ],
            parameters=[],
            chart="none",
            chart_label_dimension=None,
            chart_value_aggregation=None,
        )
        resolved, warnings = resolve_spec_names(spec, self.user)
        self.assertEqual(warnings, [])
        self.assertEqual(
            set(resolved.filters[0].value),
            {"INDICE FACILITY SERVICES, S.L.", "MICROCLIMA, S.A."},
        )
        result = ReportCompiler().compile(resolved, self.user)
        self.assertEqual(
            {row["client"] for row in result.rows},
            {"INDICE FACILITY SERVICES, S.L.", "MICROCLIMA, S.A."},
        )


class NameResolverTests(ReportTestCase):
    def test_matches_typo_and_case(self):
        names = ["San Carlos Aluminio S.L.", "Beta Co", "Alpha"]
        self.assertEqual(
            best_name_match("san carlos", names),
            "San Carlos Aluminio S.L.",
        )
        self.assertEqual(
            best_name_match("San Carloz Aluminio", names),
            "San Carlos Aluminio S.L.",
        )

    def test_unknown_client_filter_is_dropped_with_a_warning(self):
        # An unknown client name is dropped (never rewritten as the company
        # name, never left to produce an empty result) and reported once.
        for company_name, tax_id, username, known_client, value in (
            ("San Carlos Plant", "SC1", "resolver", "Other Client", "san carlos plant"),
            ("Only Plant", "OP1", "dropclient", "Local Client", "Facility Manager Service"),
        ):
            with self.subTest(value=value):
                company = Company.objects.create(name=company_name, tax_identifier=tax_id)
                user = _dataset_user(company, username=username, is_staff=False)
                ActiveCompany.set_current_ids([company.pk])
                Client.objects.create(company=company, name=known_client)

                spec = ReportSpec.from_dict(
                    {
                        "dataset": ReportDomain.SALES_INVOICES,
                        "dimensions": ["status"],
                        "aggregations": [
                            {"measure": "*", "function": "count", "alias": "n"}
                        ],
                        "filters": [
                            {
                                "field": "client",
                                "op": "eq",
                                "value": value,
                                "parameter": None,
                            }
                        ],
                        "parameters": [],
                        "chart": "none",
                        "chart_label_dimension": None,
                        "chart_value_aggregation": None,
                    }
                )
                resolved, warnings = resolve_spec_names(spec, user)
                self.assertEqual(resolved.filters, [])
                self.assertEqual(len(warnings), 1)
                self.assertIn(value, warnings[0])


class DatasetAccessTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name="Access Co", tax_identifier="AC1")
        cls.user = _dataset_user(
            cls.company, "invoices.view_salesinvoice", username="scoped"
        )

    def test_reports_only_available_datasets_flagged(self):
        access = {a.key: a.available for a in datasets_access_for_user(self.user)}
        self.assertTrue(access[ReportDomain.SALES_INVOICES])
        self.assertFalse(access[ReportDomain.QUOTES])
        self.assertFalse(access[ReportDomain.PAYMENTS])
        self.assertFalse(access[ReportDomain.PRODUCTION_TIME])
        self.assertFalse(access[ReportDomain.INVENTORY])

    def test_no_permissions_means_nothing_available(self):
        bare = _dataset_user(None, username="noaccess", is_staff=False)
        access = datasets_access_for_user(bare)
        self.assertTrue(access)
        self.assertFalse(any(a.available for a in access))


class CatalogIntegrityTests(TestCase):
    """Structural checks across every registered dataset, not just one.

    ReportCreationService.scope_to_companies (create_report.py) unconditionally injects a
    'company' filter onto every generated spec, regardless of domain —
    a dataset missing it from filter_fields fails every single report
    with "Unknown filter field: 'company'" (found live in LoansDataset).
    """

    def test_every_dataset_has_a_company_filter(self):
        missing = [
            ds.key for ds in all_datasets() if "company" not in ds.filter_map()
        ]
        self.assertEqual(missing, [])

    def test_every_dimension_key_is_unique_within_its_dataset(self):
        for ds in all_datasets():
            keys = [d.key for d in ds.dimensions]
            self.assertEqual(
                len(keys), len(set(keys)), f"duplicate dimension key in {ds.key}"
            )

    def test_every_filter_key_is_unique_within_its_dataset(self):
        for ds in all_datasets():
            keys = [f.key for f in ds.filter_fields]
            self.assertEqual(
                len(keys), len(set(keys)), f"duplicate filter key in {ds.key}"
            )

    def test_every_dataset_refuses_a_user_without_its_permission(self):
        bare = _dataset_user(None, username="nodatasetperm", is_staff=False)
        for ds in all_datasets():
            with self.subTest(dataset=ds.key):
                spec = ReportSpec.from_dict(
                    {
                        "dataset": ds.key,
                        "dimensions": [],
                        "aggregations": [
                            {"measure": "*", "function": "count", "alias": "n"}
                        ],
                        "filters": [],
                        "parameters": [],
                        "chart": "none",
                        "chart_label_dimension": None,
                        "chart_value_aggregation": None,
                    }
                )
                with self.assertRaises(ReportPermissionDenied):
                    ReportCompiler().compile(spec, bare)


class ClassifierFactoryTests(TestCase):
    def test_defaults_to_claude(self):
        self.assertIsInstance(build_classifier(), ClaudeReportClassifier)


class ClaudeReportClassifierTests(TestCase):
    @patch("apps.reports.services.claude_client.anthropic.Anthropic")
    def test_classify_parses_forced_tool_call(self, mock_anthropic_cls):
        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client
        mock_block = MagicMock(
            type="tool_use",
            input={
                "domain": ReportDomain.SALES_INVOICES,
                "confidence": 0.92,
                "needs_clarification": False,
            },
        )
        # "name" is a reserved Mock() constructor kwarg, must be set after.
        mock_block.name = "classify_report_domain"
        mock_client.messages.create.return_value = MagicMock(content=[mock_block])

        result = ClaudeReportClassifier().classify(
            "facturas pendientes de cobro",
            api_key="sk-ant-test",
        )

        self.assertEqual(result.domain, ReportDomain.SALES_INVOICES)
        self.assertEqual(result.confidence, 0.92)
        self.assertFalse(result.needs_clarification)
        self.assertEqual(result.source, "claude")
        mock_anthropic_cls.assert_called_once_with(api_key="sk-ant-test")
        called_kwargs = mock_client.messages.create.call_args.kwargs
        # Sonnet/Opus 5.5 reject tool_choice type "tool"/"any" outright —
        # only "auto" (plus a single tool + instruction) works on them.
        self.assertEqual(called_kwargs["tool_choice"], {"type": "auto"})

    def test_missing_api_key_raises(self):
        with self.assertRaises(ClaudeAPIKeyMissing):
            ClaudeReportClassifier().classify("facturas", api_key="")

    @patch("apps.reports.services.claude_client.anthropic.Anthropic")
    def test_missing_tool_call_raises(self, mock_anthropic_cls):
        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client
        mock_client.messages.create.return_value = MagicMock(content=[])

        with self.assertRaises(ClaudeClassifierError):
            ClaudeReportClassifier().classify("facturas", api_key="sk-ant-test")


class ClaudeReportSpecGeneratorTests(TestCase):
    def _mock_tool_block(self, **overrides):
        payload = {
            "dataset": ReportDomain.SALES_INVOICES,
            "dimensions": ["status"],
            "aggregations": [
                {"measure": "total_amount", "function": "sum", "alias": "total"}
            ],
            "filters": [],
            "parameters": [],
            "chart": "none",
            "chart_label_dimension": None,
            "chart_value_aggregation": None,
            "name": "Facturas por estado",
            "can_express": True,
            "summary": "Facturas agrupadas por estado.",
            "gap": "",
        }
        payload.update(overrides)
        block = MagicMock(type="tool_use", input=payload)
        block.name = "emit_report_spec"  # "name" is a reserved Mock() kwarg
        return block

    @patch("apps.reports.services.claude_client.anthropic.Anthropic")
    def test_generates_valid_spec(self, mock_anthropic_cls):
        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client
        mock_client.messages.create.return_value = MagicMock(
            content=[self._mock_tool_block()]
        )

        result = ClaudeReportSpecGenerator().generate(
            "facturas por estado",
            ReportDomain.SALES_INVOICES,
            api_key="sk-ant-test",
        )

        self.assertIsNotNone(result.spec)
        self.assertEqual(result.spec.dataset, ReportDomain.SALES_INVOICES)
        self.assertEqual(result.name, "Facturas por estado")

    @patch("apps.reports.services.claude_client.anthropic.Anthropic")
    def test_can_express_false_returns_gap_without_spec(self, mock_anthropic_cls):
        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client
        mock_client.messages.create.return_value = MagicMock(
            content=[
                self._mock_tool_block(
                    can_express=False,
                    gap="No field for that metric.",
                )
            ]
        )

        result = ClaudeReportSpecGenerator().generate(
            "algo imposible",
            ReportDomain.SALES_INVOICES,
            api_key="sk-ant-test",
        )

        self.assertIsNone(result.spec)
        self.assertEqual(result.gap, "No field for that metric.")

    @patch("apps.reports.services.claude_client.anthropic.Anthropic")
    def test_unknown_dimension_from_model_raises(self, mock_anthropic_cls):
        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client
        mock_client.messages.create.return_value = MagicMock(
            content=[self._mock_tool_block(dimensions=["not_a_real_dimension"])]
        )

        with self.assertRaises(ClaudeSpecError):
            ClaudeReportSpecGenerator().generate(
                "facturas por algo raro",
                ReportDomain.SALES_INVOICES,
                api_key="sk-ant-test",
            )

    @patch("apps.reports.services.claude_client.anthropic.Anthropic")
    def test_unknown_filter_field_is_dropped_not_fatal(self, mock_anthropic_cls):
        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client
        mock_client.messages.create.return_value = MagicMock(
            content=[
                self._mock_tool_block(
                    filters=[
                        {
                            "field": "not_a_real_filter",
                            "op": "eq",
                            "value": "x",
                            "parameter": None,
                        }
                    ]
                )
            ]
        )

        result = ClaudeReportSpecGenerator().generate(
            "facturas",
            ReportDomain.SALES_INVOICES,
            api_key="sk-ant-test",
        )

        self.assertIsNotNone(result.spec)
        self.assertEqual(result.spec.filters, [])


class ReportCreationGateTests(ReportTestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name="Gate Co", tax_identifier="G1")
        cls.user = _dataset_user(
            cls.company,
            "reports.use_reports",
            "invoices.view_salesinvoice",
            username="creator",
        )

    def test_create_without_a_company_key_names_the_company(self):
        ActiveCompany.set_current_ids([self.company.pk])
        with self.assertRaises(ReportCreationError) as ctx:
            ReportCreationService().create(
                user=self.user, prompt="facturas por estado"
            )
        self.assertIn("Gate Co", str(ctx.exception))

    @patch("apps.reports.services.claude_client.anthropic.Anthropic")
    def test_create_uses_the_company_claude_key(self, mock_anthropic_cls):
        self.company.set_claude_api_key("sk-ant-company")
        self.company.save(update_fields=["claude_api_key_encrypted"])
        classify_block = MagicMock(
            type="tool_use",
            input={
                "domain": ReportDomain.SALES_INVOICES,
                "confidence": 0.95,
                "needs_clarification": False,
            },
        )
        classify_block.name = "classify_report_domain"
        spec_block = MagicMock(
            type="tool_use",
            input={
                "dataset": ReportDomain.SALES_INVOICES,
                "dimensions": ["status"],
                "aggregations": [
                    {"measure": "total_amount", "function": "sum", "alias": "total"}
                ],
                "filters": [],
                "parameters": [],
                "chart": "none",
                "chart_label_dimension": None,
                "chart_value_aggregation": None,
                "name": "Facturas por estado",
                "can_express": True,
                "summary": "Facturas agrupadas por estado.",
                "gap": "",
            },
        )
        spec_block.name = "emit_report_spec"
        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client
        mock_client.messages.create.side_effect = [
            MagicMock(content=[classify_block]),
            MagicMock(content=[spec_block]),
        ]
        ActiveCompany.set_current_ids([self.company.pk])
        result = ReportCreationService().create(
            user=self.user, prompt="facturas por estado"
        )
        self.assertIsNotNone(result.report)
        self.assertEqual(
            [call.kwargs["api_key"] for call in mock_anthropic_cls.call_args_list],
            ["sk-ant-company", "sk-ant-company"],
        )

    def test_unsupported_domain_does_not_generate_spec(self):
        classifier = _classifier(ReportDomain.UNSUPPORTED, confidence=0.99)
        openai = MagicMock()
        suggestion_service = MagicMock()
        suggestion_service.suggest.return_value = []
        service = ReportCreationService(
            classifier=classifier,
            spec_generator=openai,
            suggestion_service=suggestion_service,
        )
        ActiveCompany.set_current_ids([self.company.pk])
        result = service.create(user=self.user, prompt="Show me the weather")
        self.assertIsNone(result.report)
        openai.generate.assert_not_called()

    def test_low_confidence_does_not_generate_spec(self):
        classifier = _classifier(ReportDomain.SALES_INVOICES, confidence=0.2)
        openai = MagicMock()
        suggestion_service = MagicMock()
        suggestion_service.suggest.return_value = []
        service = ReportCreationService(
            classifier=classifier,
            spec_generator=openai,
            suggestion_service=suggestion_service,
        )
        result = service.create(user=self.user, prompt="maybe invoices?")
        self.assertIsNone(result.report)
        openai.generate.assert_not_called()

    def test_needs_clarification_populates_suggestions(self):
        classifier = _classifier(
            ReportDomain.SALES_INVOICES, needs_clarification=True, source="jev"
        )
        openai = MagicMock()
        suggestion_service = MagicMock()
        suggestion_service.suggest.return_value = [
            "Facturas de Client A pendientes de cobro este mes"
        ]
        service = ReportCreationService(
            classifier=classifier,
            spec_generator=openai,
            suggestion_service=suggestion_service,
        )
        ActiveCompany.set_current_ids([self.company.pk])
        result = service.create(user=self.user, prompt="facturas")
        self.assertIsNone(result.report)
        self.assertEqual(
            result.suggestions,
            ["Facturas de Client A pendientes de cobro este mes"],
        )
        openai.generate.assert_not_called()
        suggestion_service.suggest.assert_called_once()
        called_prompt, called_access = suggestion_service.suggest.call_args[0]
        self.assertEqual(called_prompt, "facturas")
        self.assertTrue(
            any(a.key == ReportDomain.SALES_INVOICES and a.available for a in called_access)
        )

    def test_create_saves_spec_from_openai(self):
        classifier = _classifier(ReportDomain.SALES_INVOICES)
        openai = MagicMock()
        openai.generate.return_value = (
            _invoice_spec(),
            "Invoices by status",
        )
        service = ReportCreationService(classifier=classifier, spec_generator=openai)
        ActiveCompany.set_current_ids([self.company.pk])
        result = service.create(
            user=self.user,
            prompt="Invoices by status",
            visibility=ReportVisibility.COMPANY,
        )
        self.assertIsNotNone(result.report)
        self.assertEqual(Report.objects.count(), 1)
        self.assertEqual(result.report.name, "Invoices by status")
        self.assertEqual(result.report.domain, ReportDomain.SALES_INVOICES)
        openai.generate.assert_called_once()

    def test_describe_spec_names_the_grouping_and_period(self):
        spec = _invoice_spec(
            dimensions=["payment_method"],
            chart_label_dimension="payment_method",
            filters=[
                {
                    "field": "date_issued",
                    "op": "gte",
                    "value": "2026-01-01",
                    "parameter": "date_from",
                },
                {
                    "field": "date_issued",
                    "op": "lte",
                    "value": "2026-09-30",
                    "parameter": "date_to",
                },
            ],
            parameters=["date_from", "date_to"],
        )
        summary = describe_spec(spec)
        self.assertIn("grouped by Payment method", summary)
        self.assertIn("2026-01-01", summary)
        self.assertIn("2026-09-30", summary)

    def _generated_shell(self):
        report = Report.objects.create(
            company=self.company,
            owner=self.user,
            name="draft",
            prompt="Count invoices by payment method this year",
            domain="",
            spec={},
            visibility=ReportVisibility.PRIVATE,
            status=ReportGenerationStatus.PROCESSING,
        )
        report.included_companies.add(self.company)
        return report

    def test_apply_waits_for_confirmation_and_describes_the_spec(self):
        classifier = _classifier(ReportDomain.SALES_INVOICES)
        spec = _invoice_spec(
            dimensions=["payment_method"],
            chart_label_dimension="payment_method",
        )
        openai = MagicMock()
        openai.generate.return_value = (spec, "Invoices by payment method")
        service = ReportCreationService(classifier=classifier, spec_generator=openai)
        report = self._generated_shell()
        service.apply(report)
        report.refresh_from_db()
        self.assertEqual(report.status, ReportGenerationStatus.CONFIRM)
        self.assertIn("grouped by Payment method", report.generation_summary)
        self.assertEqual(report.spec["dimensions"], ["payment_method"])

    def test_apply_records_a_catalog_gap_without_substituting_a_spec(self):
        classifier = _classifier(ReportDomain.SALES_INVOICES)
        openai = MagicMock()
        openai.generate.return_value = SpecGeneration(
            spec=None,
            name="Untitled",
            gap="No field for payment terms.",
        )
        service = ReportCreationService(classifier=classifier, spec_generator=openai)
        report = self._generated_shell()
        service.apply(report)
        report.refresh_from_db()
        self.assertEqual(report.status, ReportGenerationStatus.ERROR)
        self.assertEqual(report.spec, {})
        self.assertEqual(report.generation_error, "No field for payment terms.")

    def _service_for_company_choice(self):
        classifier = _classifier(ReportDomain.SALES_INVOICES)
        openai = MagicMock()
        openai.generate.return_value = (_invoice_spec(), "Invoices by status")
        return ReportCreationService(classifier=classifier, spec_generator=openai)

    def test_multiple_active_companies_require_an_explicit_company(self):
        other = Company.objects.create(name="Gate Co 2", tax_identifier="G2")
        CompanyUser.objects.create(user=self.user, company=other, is_active=True)
        cache.clear()
        ActiveCompany.set_current_ids([self.company.pk, other.pk])
        with self.assertRaises(ReportCreationError):
            self._service_for_company_choice().create(
                user=self.user,
                prompt="Invoices by status",
            )

    def test_explicit_company_is_stored_when_several_are_active(self):
        other = Company.objects.create(name="Gate Co 3", tax_identifier="G3")
        CompanyUser.objects.create(user=self.user, company=other, is_active=True)
        cache.clear()
        ActiveCompany.set_current_ids([self.company.pk, other.pk])
        result = self._service_for_company_choice().create(
            user=self.user,
            prompt="Invoices by status",
            company=other,
        )
        self.assertEqual(result.report.company_id, other.pk)

    def test_several_selected_companies_are_the_report_scope(self):
        other = Company.objects.create(name="Gate Co 4", tax_identifier="G4")
        CompanyUser.objects.create(user=self.user, company=other, is_active=True)
        cache.clear()
        ActiveCompany.set_current_ids([self.company.pk, other.pk])
        result = self._service_for_company_choice().create(
            user=self.user,
            prompt="Facturas de Gate Co y Gate Co 4 por empresa",
            companies=[self.company, other],
        )
        included = set(result.report.included_companies.values_list("name", flat=True))
        self.assertEqual(included, {"Gate Co", "Gate Co 4"})
        company_filter = next(
            filt for filt in result.report.spec["filters"] if filt["field"] == "company"
        )
        self.assertEqual(company_filter["op"], "in")
        self.assertEqual(
            set(company_filter["value"]),
            {"Gate Co", "Gate Co 4"},
        )

    def test_editing_keeps_the_record_company_when_it_stays_selected(self):
        other = Company.objects.create(name="Gate Co 5", tax_identifier="G5")
        CompanyUser.objects.create(user=self.user, company=other, is_active=True)
        cache.clear()
        ActiveCompany.set_current_ids([self.company.pk, other.pk])
        service = self._service_for_company_choice()
        created = service.create(
            user=self.user,
            prompt="Invoices by status",
            company=other,
        )
        updated = service.regenerate(
            created.report,
            "Invoices by status",
            self.user,
            companies=[self.company, other],
        )
        self.assertEqual(updated.report.company_id, other.pk)
        self.assertEqual(
            set(updated.report.included_companies.values_list("pk", flat=True)),
            {self.company.pk, other.pk},
        )

    def test_classified_domain_without_permission_raises_friendly_message(self):
        bare = _dataset_user(
            self.company, "reports.use_reports", username="unauthorized"
        )

        classifier = _classifier(ReportDomain.QUOTES)
        openai = MagicMock()
        service = ReportCreationService(classifier=classifier, spec_generator=openai)
        ActiveCompany.set_current_ids([self.company.pk])
        with self.assertRaises(PermissionDenied) as ctx:
            service.create(user=bare, prompt="Quotes by status")
        self.assertIn(str(ReportDomain.QUOTES.label), str(ctx.exception))
        openai.generate.assert_not_called()

    def test_jev_client_kwarg_still_accepted(self):
        jev = _classifier(ReportDomain.UNSUPPORTED, confidence=1.0, source="jev")
        openai = MagicMock()
        suggestion_service = MagicMock()
        suggestion_service.suggest.return_value = []
        service = ReportCreationService(
            jev_client=jev,
            spec_generator=openai,
            suggestion_service=suggestion_service,
        )
        result = service.create(user=self.user, prompt="weather")
        self.assertIsNone(result.report)
        jev.classify.assert_called_once()
        openai.generate.assert_not_called()


class ReportRegenerateTests(ReportTestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name="Regen Co", tax_identifier="RG1")
        perms = ("reports.use_reports", "invoices.view_salesinvoice")
        cls.owner = _dataset_user(cls.company, *perms, username="regenowner")
        cls.other = _dataset_user(cls.company, *perms, username="regenother")
        cls.superuser = _dataset_user(
            cls.company, *perms, username="regensuper", is_superuser=True
        )

    def _report(self):
        return Report.objects.create(
            company=self.company,
            owner=self.owner,
            name="Old name",
            prompt="old prompt",
            domain=ReportDomain.SALES_INVOICES,
            spec=_invoice_spec().to_dict(),
            visibility=ReportVisibility.PRIVATE,
        )

    def test_non_owner_non_superuser_cannot_regenerate(self):
        report = self._report()
        service = ReportCreationService(
            classifier=MagicMock(), spec_generator=MagicMock()
        )
        with self.assertRaises(PermissionDenied):
            service.regenerate(report=report, prompt="new prompt", user=self.other)

    def test_owner_and_superuser_can_regenerate(self):
        for user, new_name, new_prompt, extra, expected_visibility in (
            (self.owner, "Updated name", "updated prompt", {}, ReportVisibility.PRIVATE),
            (
                self.superuser,  # someone else's report
                "New name",
                "new prompt",
                {"visibility": ReportVisibility.COMPANY},
                ReportVisibility.COMPANY,
            ),
        ):
            with self.subTest(user=user.username):
                report = self._report()
                classifier = _classifier(ReportDomain.SALES_INVOICES)
                openai = MagicMock()
                openai.generate.return_value = (_invoice_spec(), new_name)
                service = ReportCreationService(classifier=classifier, spec_generator=openai)
                ActiveCompany.set_current_ids([self.company.pk])
                result = service.regenerate(
                    report=report, prompt=new_prompt, user=user, **extra
                )
                self.assertIsNotNone(result.report)
                report.refresh_from_db()
                self.assertEqual(report.name, new_name)
                self.assertEqual(report.prompt, new_prompt)
                self.assertEqual(report.visibility, expected_visibility)


class ReportAdminPermissionTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name="Admin Perm Co", tax_identifier="APC1")
        perm = "reports.use_reports"
        cls.owner = _dataset_user(cls.company, perm, username="ownerperm")
        cls.other = _dataset_user(cls.company, perm, username="otherperm")
        cls.superuser = _dataset_user(
            cls.company, perm, username="superperm", is_superuser=True
        )
        cls.report = Report.objects.create(
            company=cls.company,
            owner=cls.owner,
            name="Admin perm report",
            prompt="x",
            domain=ReportDomain.SALES_INVOICES,
            spec=_invoice_spec().to_dict(),
        )

    @staticmethod
    def _request(user):
        from django.test import RequestFactory

        request = RequestFactory().get("/")
        request.user = user
        return request

    @staticmethod
    def _admin():
        from django.contrib import admin as django_admin

        from apps.reports.admin import ReportAdmin

        return ReportAdmin(Report, django_admin.site)

    def test_only_owner_and_superuser_can_change_and_delete(self):
        # Company-visible so it's in every user's changelist queryset.
        company_report = Report.objects.create(
            company=self.company,
            owner=self.owner,
            name="Shared report",
            prompt="x",
            domain=ReportDomain.SALES_INVOICES,
            spec=_invoice_spec().to_dict(),
            visibility=ReportVisibility.COMPANY,
        )
        admin_instance = self._admin()
        for user, expected in (
            (self.owner, True),
            (self.other, False),
            (self.superuser, True),
        ):
            with self.subTest(user=user.username):
                request = self._request(user)
                self.assertEqual(
                    admin_instance.has_change_permission(request, self.report), expected
                )
                self.assertEqual(
                    admin_instance.has_delete_permission(request, self.report), expected
                )
                row = admin_instance.get_queryset(request).get(pk=company_report.pk)
                self.assertEqual(row._can_edit, expected)


class CreateStructuredCompletionTests(TestCase):
    @staticmethod
    def _bad_request(message: str):
        import httpx
        from openai import BadRequestError

        request = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
        response = httpx.Response(400, request=request)
        return BadRequestError(message, response=response, body=None)

    def test_no_reasoning_effort_when_unset(self):
        from apps.reports.services.openai_client import create_structured_completion

        client = MagicMock()
        with override_settings(OPENAI_REPORT_REASONING_EFFORT=""):
            create_structured_completion(client, model="gpt-4.1-mini", messages=[])
        client.chat.completions.create.assert_called_once_with(
            model="gpt-4.1-mini", messages=[]
        )

    def test_passes_reasoning_effort_when_set(self):
        from apps.reports.services.openai_client import create_structured_completion

        client = MagicMock()
        with override_settings(OPENAI_REPORT_REASONING_EFFORT="none"):
            create_structured_completion(client, model="gpt-6-luna", messages=[])
        client.chat.completions.create.assert_called_once_with(
            reasoning_effort="none", model="gpt-6-luna", messages=[]
        )

    def test_retries_without_reasoning_effort_on_unsupported_model(self):
        from apps.reports.services.openai_client import create_structured_completion

        client = MagicMock()
        client.chat.completions.create.side_effect = [
            self._bad_request(
                "Unrecognized request argument supplied: reasoning_effort"
            ),
            "second-call-result",
        ]
        with override_settings(OPENAI_REPORT_REASONING_EFFORT="none"):
            result = create_structured_completion(
                client, model="gpt-4.1-mini", messages=[]
            )
        self.assertEqual(result, "second-call-result")
        self.assertEqual(client.chat.completions.create.call_count, 2)

    def test_unrelated_bad_request_is_not_swallowed(self):
        from apps.reports.services.openai_client import create_structured_completion

        client = MagicMock()
        client.chat.completions.create.side_effect = self._bad_request(
            "Invalid schema"
        )
        with override_settings(OPENAI_REPORT_REASONING_EFFORT="none"):
            with self.assertRaises(Exception) as ctx:
                create_structured_completion(client, model="gpt-6-luna", messages=[])
        self.assertIn("Invalid schema", str(ctx.exception))
        self.assertEqual(client.chat.completions.create.call_count, 1)


class ClaudeReportPromptSuggesterTests(TestCase):
    def test_returns_empty_without_any_accessible_dataset(self):
        access = [
            DatasetAccess("sales_invoices", "Sales invoices", "invoices.view_salesinvoice", False)
        ]
        self.assertEqual(
            ClaudeReportPromptSuggester().suggest("algo", access, api_key="sk-ant-test"),
            [],
        )

    @patch("apps.reports.services.claude_client.anthropic.Anthropic")
    def test_returns_parsed_suggestions(self, mock_anthropic_cls):
        access = [
            DatasetAccess("sales_invoices", "Sales invoices", "invoices.view_salesinvoice", True)
        ]
        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client
        mock_block = MagicMock(
            type="tool_use",
            input={
                "suggestions": [
                    "Facturas de Client A este mes",
                    "Facturas pendientes de cobro este trimestre",
                ]
            },
        )
        # "name" is a reserved Mock() constructor kwarg, must be set after.
        mock_block.name = "suggest_report_prompts"
        mock_client.messages.create.return_value = MagicMock(content=[mock_block])

        result = ClaudeReportPromptSuggester().suggest(
            "algo vago", access, api_key="sk-ant-test"
        )

        self.assertEqual(
            result,
            [
                "Facturas de Client A este mes",
                "Facturas pendientes de cobro este trimestre",
            ],
        )

    @patch("apps.reports.services.claude_client.anthropic.Anthropic")
    def test_returns_empty_on_claude_failure(self, mock_anthropic_cls):
        access = [
            DatasetAccess("sales_invoices", "Sales invoices", "invoices.view_salesinvoice", True)
        ]
        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client
        mock_client.messages.create.side_effect = RuntimeError("boom")

        result = ClaudeReportPromptSuggester().suggest(
            "algo vago", access, api_key="sk-ant-test"
        )

        self.assertEqual(result, [])

    def test_returns_empty_without_anthropic_key(self):
        access = [
            DatasetAccess("sales_invoices", "Sales invoices", "invoices.view_salesinvoice", True)
        ]
        self.assertEqual(
            ClaudeReportPromptSuggester().suggest("algo vago", access, api_key=""),
            [],
        )


class ReportGenerationTaskTests(ReportTestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name="Task Co", tax_identifier="T1")
        cls.user = _dataset_user(cls.company, username="task-user", is_staff=False)

    def _shell_report(self):
        report = Report.objects.create(
            company=self.company,
            owner=self.user,
            name="Invoices by status",
            prompt="Invoices by status",
            domain="",
            spec={},
            visibility=ReportVisibility.PRIVATE,
            status=ReportGenerationStatus.PROCESSING,
        )
        report.included_companies.add(self.company)
        return report

    def test_task_applies_generation_inside_the_saved_companies(self):
        report = self._shell_report()
        captured = {}

        def apply(saved):
            captured["ids"] = ActiveCompany.get_current_ids()
            captured["pk"] = saved.pk

        service = MagicMock()
        service.apply.side_effect = apply
        with patch("apps.reports.tasks.ReportCreationService", return_value=service):
            result = generate_report(report.pk)

        self.assertEqual(result["status"], "ok")
        self.assertEqual(captured["pk"], report.pk)
        self.assertEqual(captured["ids"], [self.company.pk])
        self.assertIsNone(ActiveCompany.get_current_ids())

    def test_missing_report_is_an_error_result(self):
        result = generate_report(0)
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["detail"], "not found")

    def test_unexpected_failure_marks_the_report_as_error(self):
        report = self._shell_report()
        service = MagicMock()
        service.apply.side_effect = RuntimeError("boom")
        with patch("apps.reports.tasks.ReportCreationService", return_value=service):
            result = generate_report(report.pk)

        report.refresh_from_db()
        self.assertEqual(result["status"], "error")
        self.assertEqual(report.status, ReportGenerationStatus.ERROR)
        self.assertTrue(report.generation_error)
        self.assertIsNone(ActiveCompany.get_current_ids())


class ReportCreationAsyncViewTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name="Async Co", tax_identifier="A1")
        cls.user = _dataset_user(
            cls.company, "reports.use_reports", username="async-user"
        )

    def setUp(self):
        self.client.force_login(self.user)

    def test_create_saves_the_report_and_enqueues_its_id(self):
        with patch("apps.reports.views.generate_report.delay") as delay:
            response = self.client.post(
                reverse("admin:reports_report_create"),
                {
                    "prompt": "Invoices by status",
                    "visibility": ReportVisibility.PRIVATE,
                },
            )

        report = Report.objects.get()
        self.assertEqual(report.status, ReportGenerationStatus.PROCESSING)
        self.assertEqual(report.prompt, "Invoices by status")
        self.assertEqual(list(report.included_companies.all()), [self.company])
        delay.assert_called_once_with(report.pk)
        self.assertRedirects(
            response,
            reverse("admin:reports_report_changelist"),
            fetch_redirect_response=False,
        )

    def test_edit_requeues_the_same_report(self):
        report = Report.objects.create(
            company=self.company,
            owner=self.user,
            name="Old",
            prompt="old",
            domain=ReportDomain.SALES_INVOICES,
            spec={"dataset": ReportDomain.SALES_INVOICES},
            visibility=ReportVisibility.PRIVATE,
        )
        with patch("apps.reports.views.generate_report.delay") as delay:
            response = self.client.post(
                reverse("admin:reports_report_edit", args=[report.pk]),
                {
                    "prompt": "Invoices by client",
                    "visibility": ReportVisibility.PRIVATE,
                },
            )

        report.refresh_from_db()
        self.assertEqual(Report.objects.count(), 1)
        self.assertEqual(report.prompt, "Invoices by client")
        self.assertEqual(report.status, ReportGenerationStatus.PROCESSING)
        delay.assert_called_once_with(report.pk)
        self.assertRedirects(
            response,
            reverse("admin:reports_report_changelist"),
            fetch_redirect_response=False,
        )

    def test_edit_shows_saved_suggestions_when_the_prompt_needs_detail(self):
        report = Report.objects.create(
            company=self.company,
            owner=self.user,
            name="invoices",
            prompt="invoices",
            domain="",
            spec={},
            visibility=ReportVisibility.PRIVATE,
            status=ReportGenerationStatus.CLARIFY,
            generation_error="Need more detail",
            generation_suggestions=["Invoices by client this month"],
        )
        response = self.client.get(
            reverse("admin:reports_report_edit", args=[report.pk])
        )
        self.assertContains(response, "Invoices by client this month")
        self.assertContains(response, "Need more detail")

    def _confirmable_report(self):
        spec = _invoice_spec(
            dimensions=["payment_method"],
            chart_label_dimension="payment_method",
        )
        report = Report.objects.create(
            company=self.company,
            owner=self.user,
            name="Invoices by payment method",
            prompt="Count invoices by payment method this year",
            domain=ReportDomain.SALES_INVOICES,
            spec=spec.to_dict(),
            visibility=ReportVisibility.PRIVATE,
            status=ReportGenerationStatus.CONFIRM,
            generation_summary="the model invented this",
        )
        report.included_companies.add(self.company)
        return report

    def test_confirm_page_shows_the_spec_summary_and_a_generate_button(self):
        report = self._confirmable_report()
        response = self.client.get(
            reverse("admin:reports_report_run", args=[report.pk])
        )
        self.assertContains(response, "grouped by Payment method")
        self.assertContains(response, "Generate report")
        self.assertNotContains(response, "the model invented this")

    def test_confirm_post_marks_the_report_ready(self):
        report = self._confirmable_report()
        response = self.client.post(
            reverse("admin:reports_report_confirm", args=[report.pk])
        )
        report.refresh_from_db()
        self.assertEqual(report.status, ReportGenerationStatus.READY)
        self.assertRedirects(
            response,
            reverse("admin:reports_report_run", args=[report.pk]),
            fetch_redirect_response=False,
        )

    def test_catalog_gap_page_explains_the_limit_without_a_generate_button(self):
        report = Report.objects.create(
            company=self.company,
            owner=self.user,
            name="draft",
            prompt="Invoices by payment terms",
            domain=ReportDomain.SALES_INVOICES,
            spec={},
            visibility=ReportVisibility.PRIVATE,
            status=ReportGenerationStatus.ERROR,
            generation_error="The catalog cannot group by payment terms.",
        )
        response = self.client.get(
            reverse("admin:reports_report_run", args=[report.pk])
        )
        self.assertContains(response, "The catalog cannot group by payment terms.")
        self.assertNotContains(response, "Generate report")
        self.client.post(reverse("admin:reports_report_confirm", args=[report.pk]))
        report.refresh_from_db()
        self.assertEqual(report.status, ReportGenerationStatus.ERROR)


class ConfigurationsDatasetTests(ReportTestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name="Config Co", tax_identifier="CFG1")
        cls.user = _dataset_user(
            cls.company, "configurator.view_configuration", username="configreporter"
        )

        cls.product = ProductTemplate.objects.create(
            company=cls.company, name="Window Type A"
        )
        cls.draft_config = Configuration.objects.create(
            company=cls.company,
            name="Draft config",
            product=cls.product,
            status=ConfigurationStatus.DRAFT,
            total_price=Money("100.00", "EUR"),
        )
        cls.completed_config = Configuration.objects.create(
            company=cls.company,
            name="Completed config",
            product=cls.product,
            status=ConfigurationStatus.COMPLETED,
            total_price=Money("250.00", "EUR"),
        )

    spec_defaults = {
        "dataset": ReportDomain.CONFIGURATIONS,
        "dimensions": ["status"],
        "aggregations": [_agg("total_price", "sum", "total")],
    }

    def test_groups_by_status_and_sums_total_price(self):
        ActiveCompany.set_current_ids([self.company.pk])
        result = ReportCompiler().compile(self._spec(), self.user)
        totals = {row["status"]: row["total"] for row in result.rows}
        self.assertEqual(totals[ConfigurationStatus.DRAFT], 100.0)
        self.assertEqual(totals[ConfigurationStatus.COMPLETED], 250.0)


class MaterialsDatasetTests(ReportTestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name="Material Co", tax_identifier="MAT1")
        cls.user = _dataset_user(
            cls.company, "materials.view_material", username="materialreporter"
        )

        cls.category = Category.objects.create(company=cls.company, name="Perfiles")
        Material.objects.create(
            company=cls.company,
            reference_code="MAT-1",
            name="Cerco 50mm",
            category=cls.category,
            cost_per_unit=Decimal("4.50"),
        )
        Material.objects.create(
            company=cls.company,
            reference_code="MAT-2",
            name="Lama 200",
            category=cls.category,
            cost_per_unit=Decimal("9.00"),
        )

    spec_defaults = {
        "dataset": ReportDomain.MATERIALS,
        "dimensions": [],
        "aggregations": [_agg("cost_per_unit", "avg", "avg_cost")],
    }

    def test_average_cost_per_unit(self):
        ActiveCompany.set_current_ids([self.company.pk])
        result = ReportCompiler().compile(self._spec(), self.user)
        self.assertEqual(result.rows[0]["avg_cost"], 6.75)

    def test_reference_code_filter(self):
        ActiveCompany.set_current_ids([self.company.pk])
        spec = self._spec(
            dimensions=["reference_code"],
            aggregations=[{"measure": "*", "function": "count", "alias": "n"}],
            filters=[
                {
                    "field": "reference_code",
                    "op": "eq",
                    "value": "MAT-1",
                    "parameter": None,
                }
            ],
        )
        result = ReportCompiler().compile(spec, self.user)
        self.assertEqual([row["reference_code"] for row in result.rows], ["MAT-1"])


class AssetsDatasetTests(ReportTestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name="Asset Co", tax_identifier="AST1")
        cls.user = _dataset_user(
            cls.company, "assets.view_asset", username="assetreporter"
        )

        cls.asset_type = AssetType.objects.create(company=cls.company, name="Truck")
        cls.active_asset = Asset.objects.create(
            company=cls.company,
            asset_type=cls.asset_type,
            name="Truck 1",
            is_active=True,
        )
        cls.inactive_asset = Asset.objects.create(
            company=cls.company,
            asset_type=cls.asset_type,
            name="Truck 2",
            is_active=False,
        )

        # Category fixtures live in their own company so the is_active
        # filter above only ever sees the two trucks.
        cls.vehicle_company = Company.objects.create(
            name="Vehicle Co", tax_identifier="VEH1"
        )
        CompanyUser.objects.create(
            user=cls.user, company=cls.vehicle_company, is_active=True
        )
        truck_type = AssetType.objects.create(
            company=cls.vehicle_company,
            name="Camión",
            category=AssetTypeCategory.TRUCK,
        )
        machine_type = AssetType.objects.create(
            company=cls.vehicle_company,
            name="Sierra",
            category=AssetTypeCategory.MACHINE,
        )
        Asset.objects.create(
            company=cls.vehicle_company, asset_type=truck_type, name="Truck 1"
        )
        Asset.objects.create(
            company=cls.vehicle_company, asset_type=machine_type, name="Machine 1"
        )

    spec_defaults = {
        "dataset": ReportDomain.ASSETS,
        "dimensions": ["name"],
        "aggregations": [_agg("*", "count", "n")],
    }

    def test_is_active_filter(self):
        ActiveCompany.set_current_ids([self.company.pk])
        spec = self._spec(
            filters=[
                {"field": "is_active", "op": "eq", "value": "true", "parameter": None}
            ],
        )
        result = ReportCompiler().compile(spec, self.user)
        self.assertEqual([row["name"] for row in result.rows], ["Truck 1"])

    def test_filters_to_trucks_only(self):
        """asset_type__category must be filterable so 'vehículos' maps to truck."""
        ActiveCompany.set_current_ids([self.vehicle_company.pk])
        spec = self._spec(
            filters=[
                {
                    "field": "asset_category",
                    "op": "eq",
                    "value": "truck",
                    "parameter": None,
                }
            ],
        )
        result = ReportCompiler().compile(spec, self.user)
        self.assertEqual([row["name"] for row in result.rows], ["Truck 1"])


class BankingDatasetTests(ReportTestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name="Bank Co", tax_identifier="BNK1")
        cls.user = _dataset_user(
            cls.company, "banking.view_banktransaction", username="bankreporter"
        )

        cls.account = BankAccount.objects.create(
            company=cls.company, bank_name="BBVA"
        )
        BankTransaction.objects.create(
            company=cls.company,
            bank_account=cls.account,
            external_id="TX-1",
            transaction_date=date(2024, 1, 5),
            amount=Money("-150.00", "EUR"),
        )
        BankTransaction.objects.create(
            company=cls.company,
            bank_account=cls.account,
            external_id="TX-2",
            transaction_date=date(2024, 1, 10),
            amount=Money("300.00", "EUR"),
        )

        # In/out split fixtures live in their own company so the net
        # movement above stays at 150.
        cls.split_company = Company.objects.create(
            name="Split Co", tax_identifier="SPLIT1"
        )
        CompanyUser.objects.create(
            user=cls.user, company=cls.split_company, is_active=True
        )
        split_account = BankAccount.objects.create(
            company=cls.split_company, bank_name="BBVA"
        )
        BankTransaction.objects.create(
            company=cls.split_company,
            bank_account=split_account,
            external_id="SPLIT-IN",
            transaction_date=date(2024, 1, 1),
            amount=Money("200.00", "EUR"),
        )
        BankTransaction.objects.create(
            company=cls.split_company,
            bank_account=split_account,
            external_id="SPLIT-OUT",
            transaction_date=date(2024, 1, 2),
            amount=Money("-80.00", "EUR"),
        )

    spec_defaults = {
        "dataset": ReportDomain.BANKING,
        "dimensions": [],
        "aggregations": [_agg("amount", "sum", "total")],
    }

    def test_sums_net_movement(self):
        ActiveCompany.set_current_ids([self.company.pk])
        result = ReportCompiler().compile(self._spec(), self.user)
        self.assertEqual(result.rows[0]["total"], 150.0)

    def test_amount_in_and_out_are_separated(self):
        """amount_in/amount_out must separate the signed amount by direction."""
        ActiveCompany.set_current_ids([self.split_company.pk])
        spec = self._spec(
            aggregations=[
                _agg("amount_in", "sum", "money_in"),
                _agg("amount_out", "sum", "money_out"),
            ],
        )
        result = ReportCompiler().compile(spec, self.user)
        self.assertEqual(result.rows[0]["money_in"], 200.0)
        self.assertEqual(result.rows[0]["money_out"], 80.0)


class FinanceDatasetTests(ReportTestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name="Finance Co", tax_identifier="FIN1")
        cls.user = _dataset_user(
            cls.company, "finance.view_taxperiod", username="financereporter"
        )

        tax_type = TaxType.objects.create(tax_name="IVA")
        rule = TaxScheduleRule.objects.create(
            company=cls.company,
            name="IVA Trimestral",
            tax_type=tax_type,
            start_date=date(2024, 1, 1),
            period_length=3,
            payment_day=20,
        )
        # TaxScheduleRule.save() auto-generates every period from start_date
        # up to today (finance/models.py:143 generate_up_to_today) — fetch
        # the one covering 2024 Q1 and fill it in rather than creating a
        # second row that collides on the (rule, date_from) unique
        # constraint.
        cls.period = TaxPeriod.objects.get(rule=rule, date_from=date(2024, 1, 1))
        cls.period.tax_collected = Money("1000.00", "EUR")
        cls.period.tax_paid = Money("400.00", "EUR")
        cls.period.tax_net = Money("600.00", "EUR")
        cls.period.net_after_credit = Money("600.00", "EUR")
        cls.period.save()

    spec_defaults = {
        "dataset": ReportDomain.FINANCE,
        "dimensions": ["rule"],
        "aggregations": [_agg("net_after_credit", "sum", "owed")],
    }

    def test_net_after_credit_measure(self):
        ActiveCompany.set_current_ids([self.company.pk])
        result = ReportCompiler().compile(self._spec(), self.user)
        self.assertEqual(result.rows[0]["owed"], 600.0)


class AccountingDatasetTests(ReportTestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(
            name="Accounting Co", tax_identifier="ACC1"
        )
        cls.user = _dataset_user(
            cls.company,
            "accounting.view_journalentryline",
            username="accountingreporter",
        )

        cls.account = CompanyAccount.objects.create(
            company=cls.company, code="430000", name="Clientes"
        )
        journal = Journal.objects.create(
            company=cls.company, name="Ventas", code="VTA", type=JournalType.SALES
        )
        entry = JournalEntry.objects.create(
            company=cls.company, journal=journal, date=date(2024, 1, 15)
        )
        JournalEntryLine.objects.create(
            company=cls.company,
            entry=entry,
            account=cls.account,
            description="Factura cliente",
            debit=Money("500.00", "EUR"),
            credit=Money("0.00", "EUR"),
        )
        JournalEntryLine.objects.create(
            company=cls.company,
            entry=entry,
            account=cls.account,
            description="Abono",
            debit=Money("0.00", "EUR"),
            credit=Money("120.00", "EUR"),
        )

    spec_defaults = {
        "dataset": ReportDomain.ACCOUNTING,
        "dimensions": ["account_code"],
        "aggregations": [_agg("debit", "sum", "total_debit")],
    }

    def test_debit_sum_by_account(self):
        ActiveCompany.set_current_ids([self.company.pk])
        result = ReportCompiler().compile(self._spec(), self.user)
        self.assertEqual(result.rows[0]["total_debit"], 500.0)
        self.assertIsNone(result.row_pks[0])

    def test_balance_is_debit_minus_credit(self):
        """balance must equal debit minus credit, summed across lines."""
        ActiveCompany.set_current_ids([self.company.pk])
        spec = self._spec(aggregations=[_agg("balance", "sum", "balance")])
        result = ReportCompiler().compile(spec, self.user)
        self.assertEqual(result.rows[0]["balance"], 380.0)


class PayrollDatasetTests(ReportTestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(
            name="Payroll Co", tax_identifier="PAY1"
        )
        cls.user = _dataset_user(
            cls.company, "payroll.view_employeepayroll", username="payrollreporter"
        )

        cls.employee = Employee.objects.create(
            company=cls.company,
            first_name="Ana",
            last_name="Lopez",
            hired_at=date(2020, 1, 1),
        )
        period = PayrollPeriod.objects.create(
            company=cls.company, year=2024, month=1
        )
        EmployeePayroll.objects.create(
            period=period,
            employee=cls.employee,
            company=cls.company,
            gross_salary=Decimal("2000.00"),
            net_salary=Decimal("1600.00"),
            employer_cost=Decimal("2600.00"),
        )

    spec_defaults = {
        "dataset": ReportDomain.PAYROLL,
        "dimensions": ["employee"],
        "aggregations": [_agg("gross_salary", "sum", "total")],
    }

    def test_gross_salary_by_employee(self):
        ActiveCompany.set_current_ids([self.company.pk])
        result = ReportCompiler().compile(self._spec(), self.user)
        self.assertEqual(result.rows[0]["employee"], "Ana Lopez")
        self.assertEqual(result.rows[0]["total"], 2000.0)


class LoansDatasetTests(ReportTestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name="Loan Co", tax_identifier="LOAN1")
        cls.user = _dataset_user(
            cls.company, "loans.view_loaninstallment", username="loanreporter"
        )

        bank = Bank.objects.create(name="Test Bank")
        bank_account = CompanyBankAccount.objects.create(
            company=cls.company, bank_name="Test Bank"
        )
        loan = Loan.objects.create(
            company=cls.company,
            bank=bank,
            bank_account=bank_account,
            name="Loan 1",
            principal_amount=Money("1000.00", "EUR"),
            interest_rate=Decimal("5.000"),
            amortization_method="french",
            start_date=date(2024, 1, 1),
            term_months=12,
        )
        cls.unpaid = LoanInstallment.objects.create(
            loan=loan,
            installment_number=1,
            due_date=date(2024, 2, 1),
            principal_due=Money("80.00", "EUR"),
            interest_due=Money("20.00", "EUR"),
            total_due=Money("100.00", "EUR"),
            outstanding_principal_after=Money("920.00", "EUR"),
            payment_status=LoanInstallmentPaymentStatus.UNPAID,
        )
        cls.paid = LoanInstallment.objects.create(
            loan=loan,
            installment_number=2,
            due_date=date(2024, 3, 1),
            principal_due=Money("80.00", "EUR"),
            interest_due=Money("20.00", "EUR"),
            total_due=Money("100.00", "EUR"),
            amount_paid=Money("100.00", "EUR"),
            outstanding_principal_after=Money("840.00", "EUR"),
            payment_status=LoanInstallmentPaymentStatus.PAID,
        )

    spec_defaults = {
        "dataset": ReportDomain.LOANS,
        "dimensions": ["installment_number"],
        "aggregations": [_agg("total_due", "sum", "total")],
    }

    def test_unpaid_filter(self):
        ActiveCompany.set_current_ids([self.company.pk])
        spec = self._spec(
            filters=[
                {
                    "field": "payment_status",
                    "op": "eq",
                    "value": "unpaid",
                    "parameter": None,
                }
            ],
        )
        result = ReportCompiler().compile(spec, self.user)
        self.assertEqual([row["installment_number"] for row in result.rows], [1])

    def test_no_row_link_since_no_standalone_admin(self):
        ActiveCompany.set_current_ids([self.company.pk])
        result = ReportCompiler().compile(self._spec(), self.user)
        self.assertTrue(all(pk is None for pk in result.row_pks))


class VacationsDatasetTests(ReportTestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name="Vacation Co", tax_identifier="VAC1")
        cls.user = _dataset_user(
            cls.company, "vacations.view_vacationrequest", username="vacationreporter"
        )

        cls.employee = Employee.objects.create(
            company=cls.company,
            first_name="Carlos",
            last_name="Ruiz",
            hired_at=date(2021, 1, 1),
        )
        # requested_days is recomputed from start/end date on save
        # (vacations/models.py VacationRequest.save -> compute_working_days),
        # so the value passed in here is informational only.
        VacationRequest.objects.create(
            employee=cls.employee,
            start_date=date(2024, 6, 1),
            end_date=date(2024, 6, 5),
            status=VacationRequestStatus.APPROVED,
            reason=AbsenceReason.ANNUAL,
        )
        VacationRequest.objects.create(
            employee=cls.employee,
            start_date=date(2024, 7, 1),
            end_date=date(2024, 7, 1),
            status=VacationRequestStatus.PENDING,
            reason=AbsenceReason.PERSONAL,
        )

    spec_defaults = {
        "dataset": ReportDomain.VACATIONS,
        "dimensions": ["status"],
        "aggregations": [_agg("requested_days", "sum", "days")],
    }

    def test_groups_by_status(self):
        ActiveCompany.set_current_ids([self.company.pk])
        result = ReportCompiler().compile(self._spec(), self.user)
        totals = {row["status"]: row["days"] for row in result.rows}
        # 2024-06-01 (Sat) to 2024-06-05 (Wed) -> 3 working days.
        self.assertEqual(totals[VacationRequestStatus.APPROVED], 3.0)
        # 2024-07-01 (Mon), a single working day.
        self.assertEqual(totals[VacationRequestStatus.PENDING], 1.0)


class FeedbackDatasetTests(ReportTestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(
            name="Feedback Co", tax_identifier="FBK1"
        )
        cls.user = _dataset_user(
            cls.company, "feedback.view_feedbackresponse", username="feedbackreporter"
        )

        client = Client.objects.create(company=cls.company, name="Feedback Client")
        quote = Quote.objects.create(
            company=cls.company, client=client, status=QuoteStatus.SENT
        )
        questionnaire = FeedbackQuestionnaire.objects.create(company=cls.company)
        FeedbackResponse.objects.create(
            company=cls.company,
            questionnaire=questionnaire,
            quote=quote,
            call_date=date(2024, 5, 10),
        )

    spec_defaults = {
        "dataset": ReportDomain.FEEDBACK,
        "dimensions": ["client"],
        "aggregations": [_agg("*", "count", "n")],
    }

    def test_counts_by_client(self):
        ActiveCompany.set_current_ids([self.company.pk])
        result = ReportCompiler().compile(self._spec(), self.user)
        self.assertEqual(result.rows[0]["client"], "Feedback Client")
        self.assertEqual(result.rows[0]["n"], 1)


class ExpensesDatasetTests(ReportTestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(
            name="Expense Co", tax_identifier="EXPC1"
        )
        cls.user = _dataset_user(
            cls.company, "expenses.view_expenseclaim", username="expensereporter"
        )

        submitter = User.objects.create_user(username="claimant", password="pass")
        ExpenseClaim.objects.create(
            company=cls.company,
            submitted_by=submitter,
            status=ExpenseClaimStatus.SUBMITTED,
            total_claimed=Money("150.00", "EUR"),
        )

    spec_defaults = {
        "dataset": ReportDomain.EXPENSES,
        "dimensions": ["status"],
        "aggregations": [_agg("total_claimed", "sum", "total")],
    }

    def test_total_claimed_by_status(self):
        ActiveCompany.set_current_ids([self.company.pk])
        result = ReportCompiler().compile(self._spec(), self.user)
        totals = {row["status"]: row["total"] for row in result.rows}
        self.assertEqual(totals[ExpenseClaimStatus.SUBMITTED], 150.0)


class EInvoicingDatasetTests(ReportTestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(
            name="EInvoicing Co", tax_identifier="EINV1"
        )
        cls.user = _dataset_user(
            cls.company,
            "einvoicing.view_einvoicingsubmission",
            username="einvoicingreporter",
        )

        client = Client.objects.create(company=cls.company, name="EInv Client")
        invoice = SalesInvoice.objects.create(
            company=cls.company,
            client=client,
            invoice_number="EINV-1",
            date_issued=date(2024, 1, 1),
            status=SalesInvoiceStatus.FINAL,
        )
        account = EInvoicingAccount.objects.create(
            company=cls.company, name="Main account"
        )
        EInvoicingSubmission.objects.create(
            account=account,
            sales_invoice=invoice,
            action=SubmissionAction.CREATE,
            status=SubmissionStatus.ERROR,
            error_message="Timeout",
        )

    spec_defaults = {
        "dataset": ReportDomain.EINVOICING,
        "dimensions": ["status"],
        "aggregations": [_agg("*", "count", "n")],
    }

    def test_counts_errored_submissions(self):
        ActiveCompany.set_current_ids([self.company.pk])
        spec = self._spec(
            filters=[
                {
                    "field": "status",
                    "op": "eq",
                    "value": "error",
                    "parameter": None,
                }
            ],
        )
        result = ReportCompiler().compile(spec, self.user)
        self.assertEqual(result.rows[0]["n"], 1)


class ProductionTimeDatasetTests(ReportTestCase):
    """duration_hours must agree with duration_seconds / 3600."""

    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name="Prod Co", tax_identifier="PRODT1")
        cls.user = _dataset_user(
            cls.company,
            "production.view_productionitemsession",
            username="prodreporter",
        )

        product = ProductTemplate.objects.create(company=cls.company, name="Ventana")
        client = Client.objects.create(company=cls.company, name="Acme")
        project_file = ProjectFile.objects.create(
            company=cls.company, name="PF1", client=client
        )
        config = Configuration.objects.create(company=cls.company, product=product)
        ct = ContentType.objects.get_for_model(Configuration)
        ticket = ProductionTicket.objects.create(
            company=cls.company,
            content_type=ct,
            object_id=config.id,
            project_file=project_file,
        )
        cls.item = ProductionItem.objects.create(
            ticket=ticket,
            company=cls.company,
            planning={},
            production_stages_order=["corte"],
        )
        start = datetime(2024, 1, 1, 9, 0, tzinfo=dt_timezone.utc)
        ProductionItemSession.objects.create(
            production_item=cls.item,
            company=cls.company,
            production_stage="corte",
            status=ProductionItemSessionStatus.DONE,
            start_time=start,
            end_time=start + timedelta(hours=2, minutes=30),
        )

    spec_defaults = {"dataset": ReportDomain.PRODUCTION_TIME}

    def test_duration_hours_matches_duration_seconds(self):
        ActiveCompany.set_current_ids([self.company.pk])
        spec = self._spec(
            aggregations=[
                _agg("duration_hours", "sum", "hours"),
                _agg("duration_seconds", "sum", "seconds"),
            ],
        )
        result = ReportCompiler().compile(spec, self.user)
        self.assertAlmostEqual(result.rows[0]["hours"], 2.5)
        self.assertAlmostEqual(result.rows[0]["seconds"] / 3600, 2.5)

    def test_duration_hours_is_rounded_for_display(self):
        ActiveCompany.set_current_ids([self.company.pk])
        start = datetime(2024, 2, 1, 9, 0, tzinfo=dt_timezone.utc)
        ProductionItemSession.objects.create(
            production_item=self.item,
            company=self.company,
            production_stage="corte",
            status=ProductionItemSessionStatus.DONE,
            start_time=start,
            end_time=start + timedelta(seconds=326),
        )
        spec = self._spec(
            aggregations=[
                _agg("duration_hours", "sum", "hours"),
            ],
        )
        result = ReportCompiler().compile(spec, self.user)
        self.assertEqual(result.rows[0]["hours"], 2.59)

    def test_count_distinct_days_skips_zero_hour_days_when_filtered(self):
        ActiveCompany.set_current_ids([self.company.pk])
        empty = datetime(2024, 1, 2, 9, 0, tzinfo=dt_timezone.utc)
        ProductionItemSession.objects.create(
            production_item=self.item,
            company=self.company,
            production_stage="corte",
            status=ProductionItemSessionStatus.DONE,
            start_time=empty,
            end_time=empty,
        )
        spec = self._spec(
            aggregations=[
                _agg("duration_hours", "sum", "hours"),
                _agg("start_date", "count_distinct", "days"),
            ],
            filters=[{"field": "duration_hours", "op": "gt", "value": "0"}],
        )
        result = ReportCompiler().compile(spec, self.user)
        self.assertEqual(result.rows[0]["days"], 1)
        self.assertAlmostEqual(result.rows[0]["hours"], 2.5)

    def test_labor_hours_counts_overlapping_sessions_once(self):
        ActiveCompany.set_current_ids([self.company.pk])
        ana = Employee.objects.create(
            company=self.company,
            first_name="Ana",
            last_name="Ruiz",
            hired_at=date(2024, 1, 1),
        )
        bea = Employee.objects.create(
            company=self.company,
            first_name="Bea",
            last_name="Luz",
            hired_at=date(2024, 1, 1),
        )
        start = datetime(2024, 3, 1, 9, 0, tzinfo=dt_timezone.utc)
        spans = (
            (ana, start, start + timedelta(hours=3)),
            (ana, start + timedelta(hours=1), start + timedelta(hours=4)),
            (ana, start + timedelta(hours=1, minutes=30), start + timedelta(hours=2)),
            (bea, start, start + timedelta(hours=2)),
        )
        for employee, opened, closed in spans:
            ProductionItemSession.objects.create(
                production_item=self.item,
                company=self.company,
                production_stage="corte",
                status=ProductionItemSessionStatus.DONE,
                start_time=opened,
                end_time=closed,
                created_by=employee,
            )
        spec = self._spec(
            dimensions=["employee"],
            aggregations=[
                _agg("labor_hours", "sum", "hours"),
                _agg("duration_hours", "sum", "raw"),
                _agg("id", "count", "n"),
            ],
            filters=[{"field": "labor_hours", "op": "gt", "value": "0"}],
        )
        result = ReportCompiler().compile(spec, self.user)
        by_name = {row["employee"]: row for row in result.rows}
        self.assertAlmostEqual(by_name["Ana Ruiz"]["hours"], 4)
        self.assertAlmostEqual(by_name["Ana Ruiz"]["raw"], 6)
        self.assertEqual(by_name["Ana Ruiz"]["n"], 2)
        self.assertAlmostEqual(by_name["Bea Luz"]["hours"], 2)
        self.assertAlmostEqual(by_name["Bea Luz"]["raw"], 2)
        self.assertEqual(by_name["Bea Luz"]["n"], 1)


class DriverProfitabilityDatasetTests(ReportTestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name="San Carlos", tax_identifier="SCX")
        cls.user = _dataset_user(
            cls.company,
            "assets.view_assettransaction",
            "management.view_workentry",
            "management.view_costentry",
            username="driverreporter",
        )
        cls.user = User.objects.get(pk=cls.user.pk)

        cls.driver = Employee.objects.create(
            company=cls.company,
            first_name="Luis",
            last_name="Garcia",
            hired_at=date(2020, 1, 1),
        )
        other = Employee.objects.create(
            company=cls.company,
            first_name="Ana",
            last_name="Ruiz",
            hired_at=date(2020, 1, 1),
        )
        client = Client.objects.create(company=cls.company, name="Obra Norte")
        project_file = ProjectFile.objects.create(
            company=cls.company, name="Obra", client=client
        )
        revenue_type = WorkEntryType.objects.create(
            company=cls.company,
            name="Porte",
            direction=WorkEntryDirection.REVENUE,
        )
        cost_type = WorkEntryType.objects.create(
            company=cls.company,
            name="Coste porte",
            direction=WorkEntryDirection.COST,
        )
        cls.trip = AssetTransaction.objects.create(
            company=cls.company,
            project_file=project_file,
            transaction_type=TransactionTypeChoices.PORTE_DELIVERY,
            performed_by=cls.driver,
            date=datetime(2026, 3, 4, 9, 0, tzinfo=dt_timezone.utc),
        )
        AssetTransaction.objects.create(
            company=cls.company,
            project_file=project_file,
            transaction_type=TransactionTypeChoices.PORTE_DELIVERY,
            performed_by=other,
            date=datetime(2026, 3, 4, 11, 0, tzinfo=dt_timezone.utc),
        )
        WorkEntry.objects.create(
            company=cls.company,
            project_file=project_file,
            entry_type=revenue_type,
            description="Porte Luis",
            quantity=Decimal("1"),
            unit_price=Money("80.00", "EUR"),
            date=date(2026, 3, 4),
            source=cls.trip,
        )
        CostEntry.objects.create(
            company=cls.company,
            project_file=project_file,
            entry_type=cost_type,
            description="Coste Luis",
            quantity=Decimal("1"),
            unit_price=Money("30.00", "EUR"),
            date=date(2026, 3, 4),
            source=cls.trip,
        )
        WorkEntry.objects.create(
            company=cls.company,
            project_file=project_file,
            entry_type=revenue_type,
            description="Manual",
            quantity=Decimal("1"),
            unit_price=Money("999.00", "EUR"),
            date=date(2026, 3, 4),
        )

    def test_trips_group_by_the_driver_who_performed_them(self):
        ActiveCompany.set_current_ids([self.company.pk])
        spec = ReportSpec.from_dict(
            {
                "dataset": ReportDomain.ASSET_TRANSACTIONS,
                "dimensions": ["driver"],
                "aggregations": [
                    {"measure": "*", "function": "count", "alias": "trips"}
                ],
                "filters": [
                    {"field": "driver", "op": "icontains", "value": "Luis Garcia"}
                ],
                "chart": "none",
            }
        )
        result = ReportCompiler().compile(spec, self.user)
        self.assertEqual(len(result.rows), 1)
        self.assertEqual(result.rows[0]["trips"], 1)
        self.assertIn("Luis", result.rows[0]["driver"])

    def test_revenue_and_cost_follow_the_drivers_trip(self):
        ActiveCompany.set_current_ids([self.company.pk])
        revenue = ReportCompiler().compile(
            ReportSpec.from_dict(
                {
                    "dataset": ReportDomain.WORK_ENTRIES,
                    "dimensions": ["driver"],
                    "aggregations": [
                        {
                            "measure": "total_price",
                            "function": "sum",
                            "alias": "revenue",
                        }
                    ],
                    "filters": [
                        {"field": "driver", "op": "icontains", "value": "Luis"}
                    ],
                    "chart": "none",
                }
            ),
            self.user,
        )
        cost = ReportCompiler().compile(
            ReportSpec.from_dict(
                {
                    "dataset": ReportDomain.COST_ENTRIES,
                    "dimensions": ["driver"],
                    "aggregations": [
                        {
                            "measure": "total_price",
                            "function": "sum",
                            "alias": "cost",
                        }
                    ],
                    "filters": [
                        {"field": "driver", "op": "icontains", "value": "Luis"}
                    ],
                    "chart": "none",
                }
            ),
            self.user,
        )
        self.assertEqual(revenue.rows[0]["revenue"], 80.0)
        self.assertEqual(cost.rows[0]["cost"], 30.0)
