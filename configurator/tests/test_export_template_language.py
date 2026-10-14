"""Export templates print in the client's language (Spanish by default): a
template in that language wins over one in no language, and the document
renders in it — dates, numbers and the context's labels included."""
from datetime import date

from django.test import TestCase

from company.models import Company, DocumentLanguage
from configurator.models import ConfigurationExportTemplate, ExportTemplateKind
from configurator.services.export.service import ConfigurationExportService
from invoices.models import (
    PaymentReminderLanguage,
    PaymentReminderSettings,
    SalesInvoice,
    SalesInvoiceStatus,
    resolve_reminder_language,
)
from sales.models import Client

BODY = "{{ date_issued|date:'F' }} | {{ document_type_label }} | {{ payment_method_display }}"


class ExportTemplateLanguageTest(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Alu SL")
        self.client_obj = Client.objects.create(name="Client SARL", company=self.company)
        self.generic = self._template("Factura", "")
        self.french = self._template("Facture", DocumentLanguage.FR)

    def _template(self, name, language):
        return ConfigurationExportTemplate.objects.create(
            name=name, kind=ExportTemplateKind.SALES_INVOICE, company=self.company,
            html=BODY, language=language,
        )

    def _default(self, language):
        return ConfigurationExportTemplate.get_default_for_product(
            product=None, kind=ExportTemplateKind.SALES_INVOICE,
            company=self.company, language=language,
        )

    def _invoice(self):
        return SalesInvoice.objects.create(
            company=self.company, client=self.client_obj, status=SalesInvoiceStatus.PROFORMA,
            date_issued=date(2026, 10, 14), payment_method="transfer",
        )

    def test_a_client_language_template_wins(self):
        self.assertEqual(self._default(DocumentLanguage.FR), self.french)

    def test_spanish_by_default_never_another_language(self):
        self.assertEqual(self._default(None), self.generic)
        self.assertEqual(self._default(DocumentLanguage.EN), self.generic)

    def test_the_document_language_is_the_clients_else_spanish(self):
        invoice = self._invoice()
        self.assertEqual(ConfigurationExportService.document_language(invoice), DocumentLanguage.ES)

        self.client_obj.language = DocumentLanguage.FR
        self.client_obj.save()
        invoice.refresh_from_db()
        self.assertEqual(ConfigurationExportService.document_language(invoice), DocumentLanguage.FR)

    def test_renders_in_the_templates_language(self):
        body, _css, _context = ConfigurationExportService.render_parts(self._invoice(), self.french)

        self.assertEqual(body.strip(), "octobre | Facture | Virement bancaire")

    def test_a_template_without_language_renders_in_the_clients(self):
        self.client_obj.language = DocumentLanguage.FR
        self.client_obj.save()

        body, _css, _context = ConfigurationExportService.render_parts(self._invoice(), self.generic)

        self.assertEqual(body.strip(), "octobre | Facture | Virement bancaire")

    def test_reminders_of_a_french_client_use_the_companys_language(self):
        self.client_obj.language = DocumentLanguage.FR
        self.client_obj.save()
        PaymentReminderSettings.objects.create(company=self.company, language=PaymentReminderLanguage.EN)

        self.assertEqual(resolve_reminder_language(self._invoice()), PaymentReminderLanguage.EN)
