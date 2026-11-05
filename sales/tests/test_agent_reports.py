"""
Agent reports: the agent_report export kind, its strategy (one agent's quotes,
invoices and commission settlement for a month) and the default templates.

    python manage.py test sales.tests.test_agent_reports
"""
import json
from datetime import date, timedelta
from decimal import Decimal
from io import BytesIO
from unittest.mock import patch

import openpyxl
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.cache import cache
from django.db import connection
from django.test import SimpleTestCase, TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone, translation
from unfold.widgets import UnfoldAdminDateWidget

from company.active_company import ActiveCompany
from company.models import CompanyUser
from configurator import default_agent_report_templates as DEFAULT_TEMPLATES
from configurator.models import ConfigurationExportTemplate, ExportTemplateKind
from configurator.services.export import ConfigurationExportService
from configurator.services.export.commissionist_agent import CommissionistAgentExportStrategy
from configurator.services.export.commissionist_agent_workbook import AgentReportSheet, AgentReportWorkbook
from configurator.services.export.kind_registry import get_preview_object
from configurator.services.export.service import CustomJSONEncoder
from core.constants import MONEY_FORMAT
from core.services.pdf_service import PDFService
from core.tasks import render_document_task
from invoices.models import SalesInvoiceStatus, TaxType
from sales.documents import render_agent_report_xlsx
from sales.models import Quote, QuoteStatus
from sales.tests.factories import make_agent, make_client, make_company, make_quote
from sales.tests.test_commission_releases import _CommissionReleaseTestCase

User = get_user_model()

AUGUST = (date(2026, 8, 1), date(2026, 8, 31))
SEPTEMBER = (date(2026, 9, 1), date(2026, 9, 30))
OCTOBER = (date(2026, 10, 1), date(2026, 10, 31))
NOVEMBER = (date(2026, 11, 1), date(2026, 11, 30))


def report(agent, period):
    return CommissionistAgentExportStrategy(agent, *period).get_context()


def workbook(agent, template, period):
    content = render_agent_report_xlsx(agent.pk, template.pk, period[0].isoformat(), period[1].isoformat())
    return openpyxl.load_workbook(BytesIO(content))


def sheet_rows(sheet):
    """The table under the agent header: column headers, then data and totals rows."""
    return [list(row) for row in sheet.iter_rows(min_row=AgentReportWorkbook.HEADER_ROW, values_only=True)]


class _AgentReportTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = make_company(name="Fistex")
        cls.other_company = make_company(name="Other")
        cls.agent = make_agent(company=cls.company, name="Gesclival")
        cls.other_agent = make_agent(company=cls.company, name="Tomás")
        cls.foreign_agent = make_agent(company=cls.other_company, name="Foreign")
        cls.client_ = make_client(company=cls.company, name="Ávila Bosch", agent=cls.agent)
        TaxType.objects.get_or_create(tax_name="IRPF", defaults={"behavior": "subtract"})

    @classmethod
    def make_quote(cls, *, agent=None, client=None, day=date(2026, 8, 6),
                   status=QuoteStatus.SENT, company=None, base="100.00", breakdown=None):
        quote = make_quote(
            company=company or cls.company, client=client, agent=agent,
            project_name=f"Job {day}", date=day, status=status,
        )
        base = Decimal(base)
        breakdown = breakdown or [
            {"tax_name": "IVA", "percentage": "21.00", "base": str(base), "tax_amount": str(base * Decimal("0.21"))},
        ]
        tax = sum(
            -Decimal(t["tax_amount"]) if t["tax_name"] == "IRPF" else Decimal(t["tax_amount"])
            for t in breakdown
        )
        # update() keeps the stored totals exactly as given.
        Quote.objects.filter(pk=quote.pk).update(
            subtotal_price=base, discounted_subtotal=base, tax_amount=tax,
            total_price=base + tax, tax_breakdown=breakdown,
        )
        return quote

    @staticmethod
    def make_default_templates(company=None):
        return {
            name: ConfigurationExportTemplate.objects.create(
                company=company, kind=ExportTemplateKind.AGENT_REPORT, name=name,
                html=html, css=DEFAULT_TEMPLATES.CSS,
            )
            for name, html in DEFAULT_TEMPLATES.TEMPLATES.items()
        }


class AgentQuotesTests(_AgentReportTestCase):
    def report_quotes(self, agent=None):
        return [row["number"] for row in report(agent or self.agent, AUGUST)["quotes"]["rows"]]

    def test_lists_the_agents_quotes_of_the_month_in_date_order(self):
        later = self.make_quote(agent=self.agent, client=self.client_, day=date(2026, 8, 20))
        earlier = self.make_quote(agent=self.agent, client=self.client_, day=date(2026, 8, 5))

        self.assertEqual(self.report_quotes(), [earlier.pk, later.pk])

    def test_unassigned_quote_of_the_agents_client_is_included(self):
        quote = self.make_quote(client=self.client_)
        Quote.objects.filter(pk=quote.pk).update(agent=None)

        self.assertEqual(self.report_quotes(), [quote.pk])

    def test_other_agents_months_drafts_and_companies_are_left_out(self):
        self.make_quote(agent=self.other_agent, client=self.client_)
        self.make_quote(agent=self.agent, client=self.client_, day=date(2026, 9, 1))
        self.make_quote(agent=self.agent, client=self.client_, status=QuoteStatus.DRAFT)
        self.make_quote(company=self.other_company, client=self.client_)

        self.assertEqual(self.report_quotes(), [])

    def test_irpf_is_split_out_of_the_taxes(self):
        self.make_quote(agent=self.agent, client=self.client_, base="1000.00", breakdown=[
            {"tax_name": "IVA", "percentage": "21.00", "base": "1000.00", "tax_amount": "210.00"},
            {"tax_name": "IRPF", "percentage": "15.00", "base": "1000.00", "tax_amount": "150.00"},
        ])

        quotes = report(self.agent, AUGUST)["quotes"]

        row = quotes["rows"][0]
        self.assertEqual((row["base"], row["retention"], row["taxes"], row["total"]),
                         (Decimal("1000.00"), Decimal("150.00"), Decimal("210.00"), Decimal("1060.00")))
        self.assertEqual(quotes["totals"]["total"], Decimal("1060.00"))

    def test_query_count_does_not_grow_with_the_rows(self):
        self.make_quote(agent=self.agent, client=self.client_)
        with CaptureQueriesContext(connection) as one_quote:
            report(self.agent, AUGUST)
        for day in (2, 3, 4):
            self.make_quote(agent=self.agent, client=self.client_, day=date(2026, 8, day))

        with self.assertNumQueries(len(one_quote)):
            report(self.agent, AUGUST)

    def test_strategy_defaults_to_the_last_closed_month(self):
        strategy = CommissionistAgentExportStrategy(self.agent)

        last_day = timezone.localdate().replace(day=1) - timedelta(days=1)
        self.assertEqual((strategy.date_from, strategy.date_to), (last_day.replace(day=1), last_day))


class AgentReportTemplateTests(_AgentReportTestCase):
    def test_default_templates_render_the_agents_data(self):
        self.make_quote(agent=self.agent, client=self.client_)
        templates = self.make_default_templates()
        context = report(self.agent, AUGUST)

        for name, template in templates.items():
            with self.subTest(name):
                html = ConfigurationExportService.render_html(self.agent, template, extra_context=context)
                self.assertIn("Gesclival", html)
        quotes_html = ConfigurationExportService.render_html(
            self.agent, templates["Presupuestos de agente"], extra_context=context,
        )
        self.assertIn("Ávila Bosch", quotes_html)

    def test_quotes_workbook_holds_the_pdfs_rows_and_totals_as_numbers(self):
        quote = self.make_quote(agent=self.agent, client=self.client_, base="1000.00", breakdown=[
            {"tax_name": "IVA", "percentage": "21.00", "base": "1000.00", "tax_amount": "210.00"},
            {"tax_name": "IRPF", "percentage": "15.00", "base": "1000.00", "tax_amount": "150.00"},
        ])
        templates = self.make_default_templates()

        book = workbook(self.agent, templates["Presupuestos de agente"], AUGUST)

        [sheet] = book.worksheets
        self.assertIn("Gesclival", sheet["A2"].value)
        header, row, totals = sheet_rows(sheet)
        self.assertEqual(len(header), 10)
        self.assertEqual(row[:5], [quote.pk, row[1], self.client_.pk, "Ávila Bosch", self.agent.pk])
        self.assertEqual(row[1].date(), date(2026, 8, 6))
        self.assertEqual(row[6:], [Decimal("1000.00"), Decimal("150.00"), Decimal("210.00"), Decimal("1060.00")])
        self.assertEqual([totals[0], *totals[6:]], [1, 1000, 150, 210, 1060])
        self.assertEqual(sheet.cell(row=AgentReportWorkbook.HEADER_ROW + 1, column=10).number_format, MONEY_FORMAT)

    def test_sheet_titles_and_headers_follow_the_active_language(self):
        template = self.make_default_templates()["Presupuestos de agente"]

        for language, title, header in (("es", "Presupuestos", "Número"), ("en", "Quotes", "Number")):
            with self.subTest(language), translation.override(language):
                [sheet] = workbook(self.agent, template, AUGUST).worksheets
                self.assertEqual((sheet.title, sheet_rows(sheet)[0][0]), (title, header))

    def test_template_editor_previews_an_agent_of_the_company(self):
        preview = get_preview_object(ExportTemplateKind.AGENT_REPORT, company_id=self.company.pk)

        self.assertEqual(preview.company, self.company)
        self.assertIn("settlement", ConfigurationExportService.get_strategy(preview).get_context())


class AgentReportWorkbookSheetsTests(SimpleTestCase):
    def test_each_default_template_exports_the_tables_it_prints(self):
        S = AgentReportSheet
        expected = {
            "Liquidación de agente": [S.SETTLEMENT],
            "Facturación de agente": [S.INVOICES, S.VAT_SUMMARY],
            "Facturación detallada de agente": [S.INVOICES, S.INVOICE_LINES, S.VAT_SUMMARY],
            "Presupuestos de agente": [S.QUOTES],
        }
        for name, html in DEFAULT_TEMPLATES.TEMPLATES.items():
            with self.subTest(name):
                self.assertEqual(AgentReportWorkbook.sheets_used_by(html), expected[name])

    def test_template_reading_no_report_data_exports_every_table(self):
        self.assertEqual(AgentReportWorkbook.sheets_used_by("<p>{{ agent.name }}</p>"), list(AgentReportSheet))


class AgentInvoicesTests(_CommissionReleaseTestCase):
    """The 1,000.00 + 21 % invoice of 29/09/2026 for a 10 % agent."""

    def test_lists_the_agents_final_invoices_of_the_month_with_their_lines_and_a_vat_summary(self):
        invoice = self.final_goods_invoice()

        invoices = report(self.agent, SEPTEMBER)["invoices"]

        [row] = invoices["rows"]
        self.assertEqual((row["base"], row["retention"], row["taxes"], row["total"]),
                         (Decimal("1000.00"), Decimal("0.00"), Decimal("210.00"), Decimal("1210.00")))
        self.assertEqual([item["description"] for item in row["items"]], [invoice.items.get().description])
        [vat] = invoices["vat_summary"]
        self.assertEqual((vat["percentage"], vat["base"], vat["amount"], vat["count"]),
                         (Decimal("21.00"), Decimal("1000.00"), Decimal("210.00"), 1))

    def test_context_serializes_for_the_template_editor(self):
        self.final_goods_invoice()

        context = json.loads(json.dumps(report(self.agent, SEPTEMBER), cls=CustomJSONEncoder))

        self.assertEqual(context["invoices"]["vat_summary"][0]["count"], 1)

    def test_detailed_invoices_workbook_has_the_invoices_their_lines_and_the_vat_summary(self):
        invoice = self.final_goods_invoice()
        template = _AgentReportTestCase.make_default_templates()["Facturación detallada de agente"]

        invoices, lines, vat = workbook(self.agent, template, SEPTEMBER).worksheets

        _header, row, _totals = sheet_rows(invoices)
        self.assertEqual(row[6:], [1000, 0, 210, 1210])
        _header, line = sheet_rows(lines)
        self.assertEqual((line[0], line[2]), (row[0], invoice.items.get().description))
        _header, group, vat_totals = sheet_rows(vat)
        self.assertEqual(group[1:], [21, 1000, 210, 1])
        self.assertEqual(vat_totals[2:4], [1000, 210])

    def test_pro_formas_other_months_and_other_agents_are_left_out(self):
        invoice = self.invoice_goods()  # still a pro forma
        other_agent = make_agent(company=self.company, name="Otro")

        self.assertEqual(report(self.agent, SEPTEMBER)["invoices"]["rows"], [])
        self.set_status(invoice, SalesInvoiceStatus.FINAL)
        self.assertEqual(report(self.agent, OCTOBER)["invoices"]["rows"], [])
        self.assertEqual(report(other_agent, SEPTEMBER)["invoices"]["rows"], [])


class AgentSettlementTests(_CommissionReleaseTestCase):
    def test_each_month_settles_what_was_collected_in_it(self):
        self.pay_in_two_months()

        self.assertEqual(report(self.agent, SEPTEMBER)["settlement"]["rows"], [])
        for month, paid_on in ((OCTOBER, date(2026, 10, 5)), (NOVEMBER, date(2026, 11, 3))):
            settlement = report(self.agent, month)["settlement"]
            [row] = settlement["rows"]
            self.assertEqual(row["paid_on"], paid_on)
            self.assertEqual((row["base"], row["percentage"], row["commission"]),
                             (Decimal("500.00"), Decimal("10.00"), Decimal("50.00")))
            self.assertEqual(settlement["totals"]["commission"], Decimal("50.00"))

    def test_settlement_workbook_settles_what_was_collected_in_the_month(self):
        self.pay_in_two_months()
        template = _AgentReportTestCase.make_default_templates()["Liquidación de agente"]

        [sheet] = workbook(self.agent, template, OCTOBER).worksheets

        _header, row, totals = sheet_rows(sheet)
        self.assertEqual(row[0].date(), date(2026, 9, 29))
        self.assertEqual(row[5:], [500, 10, 50])
        self.assertEqual(totals[5:], [500, 10, 50])

    def test_unpaid_invoice_settles_nothing(self):
        self.final_goods_invoice()

        self.assertEqual(report(self.agent, SEPTEMBER)["settlement"]["rows"], [])

    def pay_in_two_months(self):
        invoice = self.final_goods_invoice()  # issued in September
        self.pay(invoice, Decimal("605.00"), date(2026, 10, 5))
        self.pay(invoice, Decimal("605.00"), date(2026, 11, 3))
        return invoice


class AgentReportExportViewTests(_AgentReportTestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.templates = cls.make_default_templates()
        cls.foreign_template = ConfigurationExportTemplate.objects.create(
            company=cls.other_company, kind=ExportTemplateKind.AGENT_REPORT, name="Other's design", html="<p></p>",
        )

    def setUp(self):
        cache.clear()
        self.addCleanup(ActiveCompany.clear_current)
        self.user = User.objects.create_user(username="cristina", password="x", is_staff=True)
        CompanyUser.objects.create(user=self.user, company=self.company, is_active=True)
        self.user.user_permissions.add(*Permission.objects.filter(
            codename__in=["view_commissionistagent", "view_quote", "view_salesinvoice", "view_invoicecommission"],
        ))
        self.client.force_login(self.user)

    def url(self, agent):
        return reverse("admin:sales_commissionistagent_reports", args=[agent.pk])

    def download(self, agent, template, date_from="2026-08-01", date_to="2026-08-31", file_format="pdf"):
        with patch.object(PDFService, "html_to_pdf", return_value=b"%PDF fake") as pdf:
            response = self.client.post(self.url(agent), {
                "template": template.pk, "date_from": date_from, "date_to": date_to, "file_format": file_format,
                "settlement_scope": "period",
            })
        return response, pdf

    def test_page_offers_the_shared_and_own_company_templates_only(self):
        response = self.client.get(self.url(self.agent))

        self.assertEqual(response.status_code, 200)
        form = response.context["form"]
        choices = dict(form.fields["template"].choices)
        self.assertEqual(set(choices), {template.pk for template in self.templates.values()})
        last_day = timezone.localdate().replace(day=1) - timedelta(days=1)
        self.assertEqual((form["date_from"].initial, form["date_to"].initial), (last_day.replace(day=1), last_day))
        self.assertIsInstance(form.fields["date_from"].widget, UnfoldAdminDateWidget)

    def test_both_dates_are_required(self):
        response, pdf = self.download(self.agent, self.templates["Presupuestos de agente"], date_to="")

        self.assertTrue(response.context["form"].errors["date_to"])
        pdf.assert_not_called()

    def test_download_renders_the_chosen_period_with_the_chosen_template(self):
        self.make_quote(agent=self.agent, client=self.client_, base="579.60")

        # Celery runs eagerly in tests: the pdf-queue render happens inline and
        # the user is sent to the status page that downloads it.
        with patch("core.tasks.render_document_task.delay", wraps=lambda **kw: render_document_task.apply(kwargs=kw)) as delay:
            response, pdf = self.download(self.agent, self.templates["Presupuestos de agente"])

        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("core_document_status"), response.url)
        self.assertEqual(delay.call_args.kwargs["filename"], "Presupuestos de agente_Gesclival_01-08-2026_31-08-2026.pdf")
        self.assertEqual(
            delay.call_args.kwargs["kwargs"],
            {"agent_id": self.agent.pk, "template_id": self.templates["Presupuestos de agente"].pk,
             "date_from": "2026-08-01", "date_to": "2026-08-31", "settlement_scope": "period", "settlement_id": None},
        )
        self.assertIn("Ávila Bosch", pdf.call_args.args[0])

    def test_excel_download_renders_the_chosen_template_as_a_workbook(self):
        self.make_quote(agent=self.agent, client=self.client_, base="579.60")

        results = []

        def run(**kwargs):
            results.append(render_document_task.apply(kwargs=kwargs))
            return results[-1]

        with patch("core.tasks.render_document_task.delay", wraps=run) as delay:
            response, pdf = self.download(self.agent, self.templates["Presupuestos de agente"], file_format="xlsx")

        self.assertEqual(response.status_code, 302)
        task = delay.call_args.kwargs
        self.assertEqual(task["renderer"], "sales.documents.render_agent_report_xlsx")
        self.assertEqual(task["filename"], "Presupuestos de agente_Gesclival_01-08-2026_31-08-2026.xlsx")
        self.assertEqual(task["content_type"], AgentReportWorkbook.CONTENT_TYPE)
        self.assertEqual(results[0].result["status"], "ok")
        pdf.assert_not_called()

    def test_another_companys_agent_and_template_are_rejected(self):
        response, pdf = self.download(self.foreign_agent, self.templates["Presupuestos de agente"])
        self.assertEqual(response.status_code, 404)

        response, pdf = self.download(self.agent, self.foreign_template)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["form"].errors["template"])
        pdf.assert_not_called()

    def test_reports_need_the_commissions_permission(self):
        self.user.user_permissions.remove(Permission.objects.get(codename="view_invoicecommission"))

        response, pdf = self.download(self.agent, self.templates["Presupuestos de agente"])

        self.assertEqual(response.status_code, 403)
        pdf.assert_not_called()
