"""The AI classifier only offers, and only saves, accounts marked for AI classification."""
from django.test import TestCase
from djmoney.money import Money

from accounting.models import ChartOfAccountsTemplate, CompanyAccount, TemplateAccount
from accounting.services.accounting.account_classifier_interface import (
    AccountClassifierInterface,
    ClassificationBatchResponse,
    ClassificationResult,
)
from accounting.services.accounting.account_classifier_service import AccountClassifierService
from accounting.services.chart_installer import install_chart_standard
from invoices.models import InvoiceItem
from invoices.tests.factories import make_company, make_provider, make_purchase_invoice


class FakeClassifier(AccountClassifierInterface):
    """Answers every line with ``answer`` and remembers the accounts it was offered."""

    def __init__(self, answer):
        self.answer = answer
        self.offered = None

    def classify_invoices(self, invoices, account_options, company_activity_description="", accounting_framework=""):
        self.offered = [acc.code for acc in account_options]
        results = [
            ClassificationResult(invoice_id=inv["id"], line_item_id=item.id, account_code=self.answer, confident=True)
            for inv in invoices
            for item in inv["line_items"]
        ]
        return ClassificationBatchResponse(results=results, raw_response=[])


class AccountClassifierTests(TestCase):
    def setUp(self):
        self.company = make_company()
        for code, name, flagged in [
            ("600", "Compras de mercaderías", True),
            ("621", "Arrendamientos", True),
            ("700", "Ventas de mercaderías", False),
        ]:
            CompanyAccount.objects.create(company=self.company, code=code, name=name, ai_classifiable=flagged)
        invoice = make_purchase_invoice(
            company=self.company, provider=make_provider(company=self.company), total="100.00", ocr_processed=True,
        )
        self.item = InvoiceItem.objects.create(
            invoice=invoice, description="Perfil aluminio", unit_price=Money("100.00", "EUR"),
            line_total=Money("100.00", "EUR"),
        )

    def classify(self, answer):
        classifier = FakeClassifier(answer)
        AccountClassifierService(classifier).classify_unclassified_invoices()
        self.item.refresh_from_db()
        return classifier

    def test_only_flagged_accounts_are_offered(self):
        self.assertEqual(self.classify("600").offered, ["600", "621"])
        self.assertEqual(self.item.account.code, "600")

    def test_an_unflagged_answer_is_left_for_review(self):
        self.classify("700")

        self.assertIsNone(self.item.account)
        self.assertTrue(self.item.invoice.needs_manual_review)


class ChartInstallerFlagTests(TestCase):
    def test_installed_accounts_keep_the_template_flag(self):
        company = make_company()
        template = ChartOfAccountsTemplate.objects.create(name="PGC", country="ES")
        TemplateAccount.objects.create(template=template, code="600", name="Compras", ai_classifiable=True)
        TemplateAccount.objects.create(template=template, code="700", name="Ventas")

        install_chart_standard(company, template)

        flags = dict(CompanyAccount.objects.filter(company=company).values_list("code", "ai_classifiable"))
        self.assertEqual(flags, {"600": True, "700": False})
