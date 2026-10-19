import json
import time

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.exceptions import PermissionDenied
from django.test import TestCase
from django.urls import reverse

from apps.ai_agent.models import AIConversation, AIMessage
from apps.ai_agent.save_report import SaveReportError, save_report_from_message
from apps.reports.constants import ReportVisibility
from apps.reports.models import Report
from company.active_company import ActiveCompany
from company.models import Company, CompanyUser

User = get_user_model()


def _aggregate_call(company_name="San Carlos"):
    return {
        "name": "aggregate",
        "input": {
            "dataset": "sales_invoices",
            "dimensions": ["date_issued_month"],
            "aggregations": [
                {"measure": "total_amount", "function": "sum", "alias": "facturado"}
            ],
            "filters": [
                {"field": "company", "op": "icontains", "value": company_name},
            ],
            "chart": "line",
            "chart_label_dimension": "date_issued_month",
            "chart_value_aggregation": "facturado",
        },
    }


class SaveChatReportTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.san_carlos = Company.objects.create(
            name="San Carlos", tax_identifier="SC1"
        )
        cls.other = Company.objects.create(name="Otra", tax_identifier="OT1")
        cls.user = User.objects.create_user(
            username="saver", password="pass", is_staff=True
        )
        CompanyUser.objects.create(
            user=cls.user, company=cls.san_carlos, is_active=True
        )
        CompanyUser.objects.create(user=cls.user, company=cls.other, is_active=True)
        for codename in ("use_reports", "use_agent"):
            cls.user.user_permissions.add(Permission.objects.get(codename=codename))
        cls.user = User.objects.get(pk=cls.user.pk)
        cls.conversation = AIConversation.objects.create(owner=cls.user)
        AIMessage.objects.create(
            conversation=cls.conversation,
            role=AIMessage.Role.USER,
            content=[{"type": "text", "text": "Facturación de San Carlos"}],
            display_text="Facturación de San Carlos por mes",
        )
        cls.reply = AIMessage.objects.create(
            conversation=cls.conversation,
            role=AIMessage.Role.ASSISTANT,
            content=[{"type": "text", "text": "Aquí está."}],
            display_text="Aquí está.",
            tool_calls=[_aggregate_call()],
        )

    def test_saves_the_spec_against_the_named_company(self):
        report = save_report_from_message(
            user=self.user,
            message=self.reply,
            name="Facturación San Carlos",
            visibility=ReportVisibility.COMPANY,
            prompt="Facturación de San Carlos por mes",
        )
        self.assertEqual(report.company_id, self.san_carlos.pk)
        self.assertEqual(
            list(report.included_companies.values_list("pk", flat=True)),
            [self.san_carlos.pk],
        )
        self.assertEqual(report.visibility, ReportVisibility.COMPANY)
        self.assertEqual(report.spec["chart"], "line")
        self.assertEqual(report.spec["dataset"], "sales_invoices")
        self.assertEqual(report.prompt, "Facturación de San Carlos por mes")

    def test_private_report_is_stored_as_private(self):
        report = save_report_from_message(
            user=self.user,
            message=self.reply,
            name="Solo yo",
            visibility=ReportVisibility.PRIVATE,
            prompt="Facturación de San Carlos por mes",
        )
        self.assertEqual(report.visibility, ReportVisibility.PRIVATE)

    def test_a_reply_without_an_aggregate_cannot_be_saved(self):
        prose = AIMessage.objects.create(
            conversation=self.conversation,
            role=AIMessage.Role.ASSISTANT,
            content=[],
            display_text="No hay datos.",
            tool_calls=[],
        )
        with self.assertRaises(SaveReportError):
            save_report_from_message(
                user=self.user,
                message=prose,
                name="Nada",
                visibility=ReportVisibility.PRIVATE,
                prompt="hola",
            )

    def test_missing_permission_is_denied(self):
        bare = User.objects.create_user(username="noreports", password="pass")
        CompanyUser.objects.create(
            user=bare, company=self.san_carlos, is_active=True
        )
        with self.assertRaises(PermissionDenied):
            save_report_from_message(
                user=bare,
                message=self.reply,
                name="No",
                visibility=ReportVisibility.PRIVATE,
                prompt="x",
            )

    def test_view_creates_the_report_and_returns_its_url(self):
        self.client.force_login(self.user)
        session = self.client.session
        session[ActiveCompany.SESSION_KEY] = [self.san_carlos.pk]
        session[ActiveCompany.SESSION_SET_AT_KEY] = time.time()
        session.save()
        response = self.client.post(
            reverse("ai_agent:save_report"),
            data=json.dumps(
                {
                    "message_id": self.reply.pk,
                    "name": "Desde el chat",
                    "visibility": "private",
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        report = Report.objects.get(pk=payload["id"])
        self.assertEqual(report.name, "Desde el chat")
        self.assertEqual(report.owner_id, self.user.pk)
        self.assertIn(str(report.pk), payload["url"])

    def test_saves_every_dataset_and_the_chat_reply(self):
        reply = (
            "En San Carlos van 12 facturas.\n\n"
            "| Mes | Facturado |\n| --- | --- |\n| enero | 1200 |"
        )
        quotes = _aggregate_call()
        quotes["input"] = {
            **quotes["input"],
            "dataset": "quotes",
            "dimensions": ["status"],
            "aggregations": [
                {"measure": "*", "function": "count", "alias": "presupuestos"}
            ],
            "chart": "none",
            "chart_label_dimension": None,
            "chart_value_aggregation": None,
            "filters": [
                {"field": "company", "op": "icontains", "value": "Otra"},
            ],
        }
        message = AIMessage.objects.create(
            conversation=self.conversation,
            role=AIMessage.Role.ASSISTANT,
            content=[{"type": "text", "text": reply}],
            display_text=reply,
            tool_calls=[_aggregate_call(), quotes],
        )
        report = save_report_from_message(
            user=self.user,
            message=message,
            name="Facturas y presupuestos",
            visibility=ReportVisibility.PRIVATE,
            prompt="Facturas y presupuestos",
        )
        self.assertEqual(
            [section.dataset for section in report.sections()],
            ["sales_invoices", "quotes"],
        )
        self.assertEqual(report.generation_summary, reply)
        self.assertEqual(
            set(report.included_companies.values_list("name", flat=True)),
            {"San Carlos", "Otra"},
        )
