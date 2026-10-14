from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.test import TestCase
from djmoney.money import Money

from apps.ai_agent.interfaces import ChatContext, ToolExecutionError
from apps.ai_agent.models import AIArtifact, AIConversation
from apps.ai_agent.registry import ToolRegistry
from apps.ai_agent.tool_executor import ToolExecutor
from apps.ai_agent.tools.artifact_tools import GenerateArtifact
from apps.ai_agent.tools.catalog_tools import (
    Aggregate,
    DescribeDataset,
    GetEntity,
    ListDatasets,
    SearchEntities,
    TraverseRelation,
)
from apps.reports.constants import ReportDomain
from company.models import Company, CompanyUser
from invoices.models import SalesInvoice, SalesInvoiceStatus
from management.models import ProjectFile
from sales.models import Client, Quote

User = get_user_model()


class CatalogToolsTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name="Tool Co", tax_identifier="TC1")
        cls.other_company = Company.objects.create(name="Other Co", tax_identifier="OC1")
        cls.user = User.objects.create_user(
            username="tool-user", password="pass", is_staff=True
        )
        CompanyUser.objects.create(user=cls.user, company=cls.company, is_active=True)
        for app_label, codename in (
            ("sales", "view_client"),
            ("invoices", "view_salesinvoice"),
            ("management", "view_projectfile"),
        ):
            perm = Permission.objects.get(
                content_type__app_label=app_label, codename=codename
            )
            cls.user.user_permissions.add(perm)

        cls.no_perm_user = User.objects.create_user(
            username="no-perm-user", password="pass", is_staff=True
        )
        CompanyUser.objects.create(
            user=cls.no_perm_user, company=cls.company, is_active=True
        )

        cls.client_obj = Client.objects.create(company=cls.company, name="Acme SL")
        Client.objects.create(company=cls.other_company, name="Other client")
        cls.project_file = ProjectFile.objects.create(
            company=cls.company, name="EXP Tools", client=cls.client_obj
        )
        cls.invoice = SalesInvoice.objects.create(
            company=cls.company,
            client=cls.client_obj,
            project_file=cls.project_file,
            invoice_number="TOOL-1",
            total_amount=Money("1000.00", "EUR"),
            status=SalesInvoiceStatus.FINAL,
        )

    def context(self, user=None) -> ChatContext:
        return ChatContext(user_id=(user or self.user).pk)

    def test_list_datasets_includes_clients(self):
        result = ListDatasets().execute(self.context(), {})
        keys = {d["key"] for d in result["datasets"]}
        self.assertIn(ReportDomain.CLIENTS, keys)

    def test_describe_dataset_includes_relations(self):
        result = DescribeDataset().execute(
            self.context(), {"dataset": ReportDomain.PROJECT_FILES}
        )
        relation_keys = {r["key"] for r in result["relations"]}
        self.assertIn("quotes", relation_keys)
        self.assertIn("client", relation_keys)

    def test_get_entity_found(self):
        result = GetEntity().execute(
            self.context(), {"dataset": ReportDomain.CLIENTS, "id": self.client_obj.pk}
        )
        self.assertTrue(result["found"])
        self.assertEqual(result["entity"]["name"], "Acme SL")

    def test_get_entity_outside_company_scope_not_found(self):
        other_client = Client.objects.get(company=self.other_company)
        result = GetEntity().execute(
            self.context(), {"dataset": ReportDomain.CLIENTS, "id": other_client.pk}
        )
        self.assertFalse(result["found"])

    def test_get_entity_denies_without_permission(self):
        with self.assertRaises(ToolExecutionError):
            GetEntity().execute(
                self.context(self.no_perm_user),
                {"dataset": ReportDomain.CLIENTS, "id": self.client_obj.pk},
            )

    def test_traverse_relation_client_to_project_files(self):
        result = TraverseRelation().execute(
            self.context(),
            {
                "dataset": ReportDomain.CLIENTS,
                "id": self.client_obj.pk,
                "relation": "project_files",
            },
        )
        self.assertEqual([r["id"] for r in result["rows"]], [self.project_file.pk])

    def test_search_entities_filters_by_client_name(self):
        result = SearchEntities().execute(
            self.context(),
            {
                "dataset": ReportDomain.SALES_INVOICES,
                "dimensions": ["invoice_number"],
                "filters": [{"field": "client", "op": "icontains", "value": "Acme"}],
            },
        )
        self.assertEqual(result["total_count"], 1)
        self.assertEqual(result["rows"][0]["invoice_number"], "TOOL-1")

    def test_aggregate_sums_total_amount(self):
        result = Aggregate().execute(
            self.context(),
            {
                "dataset": ReportDomain.SALES_INVOICES,
                "aggregations": [
                    {"measure": "total_amount", "function": "sum", "alias": "total"}
                ],
            },
        )
        self.assertEqual(result["rows"][0]["total"], 1000.0)

    def test_aggregate_unknown_field_raises(self):
        with self.assertRaises(ToolExecutionError):
            Aggregate().execute(
                self.context(),
                {
                    "dataset": ReportDomain.SALES_INVOICES,
                    "aggregations": [
                        {"measure": "not_a_field", "function": "sum", "alias": "x"}
                    ],
                },
            )

    def test_aggregate_table_header_includes_measure_unit(self):
        result = Aggregate().execute(
            self.context(),
            {
                "dataset": ReportDomain.PROJECT_FILES,
                "aggregations": [
                    {
                        "measure": "percentage_invoiced",
                        "function": "avg",
                        "alias": "pct",
                    }
                ],
            },
        )
        self.assertIn("pct (%)", result["table"]["columns"])

    def test_aggregate_chart_payload_includes_value_unit(self):
        result = Aggregate().execute(
            self.context(),
            {
                "dataset": ReportDomain.PROJECT_FILES,
                "dimensions": ["status"],
                "aggregations": [
                    {
                        "measure": "percentage_invoiced",
                        "function": "avg",
                        "alias": "pct",
                    }
                ],
                "chart": "bar",
                "chart_label_dimension": "status",
                "chart_value_aggregation": "pct",
            },
        )
        self.assertEqual(result["chart"]["value_unit"], "%")

    def test_aggregate_omits_value_unit_for_unitless_measures(self):
        result = Aggregate().execute(
            self.context(),
            {
                "dataset": ReportDomain.SALES_INVOICES,
                "aggregations": [
                    {"measure": "total_amount", "function": "sum", "alias": "total"}
                ],
            },
        )
        self.assertNotIn("(", result["table"]["columns"][0])

    def test_tool_executor_denies_without_registered_permission(self):
        result = ToolExecutor.execute(
            "get_bank_balances", self.context(self.no_perm_user), {}
        )
        self.assertFalse(result["success"])
        self.assertIn("Permission denied", result["error"])

    def test_all_tools_are_registered(self):
        names = {t.name for t in ToolRegistry.all_tools()}
        expected = {
            "list_datasets",
            "describe_dataset",
            "get_entity",
            "traverse_relation",
            "search_entities",
            "aggregate",
            "generate_artifact",
            "get_bank_balances",
            "list_production_tickets",
            "get_production_ticket_detail",
            "production_summary",
        }
        self.assertTrue(expected.issubset(names))


class GenerateArtifactToolTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name="Artifact Co", tax_identifier="AC1")
        cls.user = User.objects.create_user(
            username="artifact-user", password="pass", is_staff=True
        )
        CompanyUser.objects.create(user=cls.user, company=cls.company, is_active=True)
        perm = Permission.objects.get(
            content_type__app_label="invoices", codename="view_salesinvoice"
        )
        cls.user.user_permissions.add(perm)
        cls.client_obj = Client.objects.create(company=cls.company, name="Artifact Client")
        SalesInvoice.objects.create(
            company=cls.company,
            client=cls.client_obj,
            invoice_number="ART-1",
            total_amount=Money("250.00", "EUR"),
            status=SalesInvoiceStatus.FINAL,
        )
        Quote.objects.create(
            company=cls.company, client=cls.client_obj, project_name="Q-1"
        )
        cls.conversation = AIConversation.objects.create(owner=cls.user)

    def context(self) -> ChatContext:
        return ChatContext(user_id=self.user.pk, conversation_id=self.conversation.pk)

    def _grant_quote_permission(self):
        perm = Permission.objects.get(
            content_type__app_label="sales", codename="view_quote"
        )
        self.user.user_permissions.add(perm)

    def _invoices_section(self, **overrides):
        section = {
            "title": "Facturas",
            "dataset": ReportDomain.SALES_INVOICES,
            "dimensions": ["invoice_number"],
            "aggregations": [
                {"measure": "total_amount", "function": "sum", "alias": "total"}
            ],
        }
        section.update(overrides)
        return section

    def _quotes_section(self, **overrides):
        section = {
            "title": "Presupuestos",
            "dataset": ReportDomain.QUOTES,
            "dimensions": ["internal_name"],
            "aggregations": [
                {"measure": "total_price", "function": "sum", "alias": "total"}
            ],
        }
        section.update(overrides)
        return section

    def _params(self, kind, sections=None):
        return {
            "kind": kind,
            "title": "Informe",
            "sections": sections or [self._invoices_section()],
        }

    def test_requires_conversation(self):
        with self.assertRaises(ToolExecutionError):
            GenerateArtifact().execute(
                ChatContext(user_id=self.user.pk), self._params("pdf")
            )

    def test_generates_pdf(self):
        result = GenerateArtifact().execute(self.context(), self._params("pdf"))
        self.assertEqual(result["row_count"], 1)
        self.assertIn("/download/", result["download_url"])

    def test_generates_excel(self):
        result = GenerateArtifact().execute(self.context(), self._params("xlsx"))
        self.assertEqual(result["row_count"], 1)

    def test_generates_pdf_with_narrative_and_chart(self):
        section = self._invoices_section(
            chart="bar",
            chart_label_dimension="invoice_number",
            chart_value_aggregation="total",
            highlights=["ART-1 is the only invoice this period."],
        )
        params = self._params("pdf", sections=[section])
        params["summary"] = "October billing is on track."

        result = GenerateArtifact().execute(self.context(), params)

        self.assertEqual(result["row_count"], 1)
        self.assertIn("/download/", result["download_url"])

    def test_generates_pdf_with_multiple_sections(self):
        self._grant_quote_permission()
        params = self._params(
            "pdf", sections=[self._invoices_section(), self._quotes_section()]
        )

        result = GenerateArtifact().execute(self.context(), params)

        self.assertEqual(result["row_count"], 2)
        self.assertEqual(len(result["sections"]), 2)
        self.assertEqual(
            {entry["dataset"] for entry in result["sections"]},
            {ReportDomain.SALES_INVOICES, ReportDomain.QUOTES},
        )

    def test_exceeds_max_sections_raises(self):
        sections = [self._invoices_section() for _ in range(6)]
        with self.assertRaises(ToolExecutionError):
            GenerateArtifact().execute(self.context(), self._params("pdf", sections=sections))

    def test_section_permission_denied_fails_whole_call(self):
        params = self._params(
            "pdf", sections=[self._invoices_section(), self._quotes_section()]
        )
        with self.assertRaises(ToolExecutionError):
            GenerateArtifact().execute(self.context(), params)
        self.assertFalse(AIArtifact.objects.filter(conversation=self.conversation).exists())
