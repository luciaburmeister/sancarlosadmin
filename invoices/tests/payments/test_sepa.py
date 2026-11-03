"""
SEPA direct debit file (invoices/sepa.py) and the admin page that downloads
it for the selected sales invoices.
"""
import time
import xml.etree.ElementTree as ET
from datetime import date, timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse
from djmoney.money import Money

from company.active_company import ActiveCompany
from invoices import sepa
from invoices.models import SalesInvoice, SalesInvoiceStatus
from invoices.tests.factories import make_bank_account, make_client, make_company
from sales.models import ClientBankAccount

NS = {"p": "urn:iso:std:iso:20022:tech:xsd:pain.008.001.02"}
DEBTOR_IBAN = "ES9121000418450200051332"
COLLECTION_DATE = date(2026, 11, 2)


class SepaFixtureMixin:
    def setUp(self):
        self.company = make_company(legal_name="Aluminios Test SL", sepa_creditor_id="ES 12 000 B12345678")
        self.creditor_account = make_bank_account(
            company=self.company, iban="ES7620770024003102575766", swift_bic="CAIXESBBXXX", is_default=True,
        )
        self.client_obj = make_client(company=self.company, name="Cliente Uno SL")
        self.mandated = self._account(self.client_obj, is_default=True)

    def _account(self, client, iban=DEBTOR_IBAN, **fields):
        return ClientBankAccount.objects.create(
            company=client.company, client=client, bank_name="Banco", iban=iban,
            sepa_mandate_reference="MANDATO-001", sepa_mandate_signed_on=date(2025, 3, 1), **fields,
        )

    def _invoice(self, total="121.00", client=None, number="F-1", **fields):
        fields.setdefault("status", SalesInvoiceStatus.FINAL)
        return SalesInvoice.objects.create(
            company=self.company, client=client or self.client_obj, invoice_number=number,
            date_issued=date(2026, 10, 1), total_amount=Money(total, "EUR"), **fields,
        )

    def _build(self, invoices, **kwargs):
        return sepa.build_direct_debit_xml(
            invoices, creditor_account=self.creditor_account, collection_date=COLLECTION_DATE, **kwargs,
        )


class BuildDirectDebitXmlTests(SepaFixtureMixin, TestCase):
    def test_one_collection_per_invoice_with_its_mandate_and_reference(self):
        first = self._invoice("121.00", number="F-1")
        second = self._invoice("50.50", number="F-2")

        xml, skipped = self._build([first, second])

        self.assertEqual(skipped, [])
        root = ET.fromstring(xml)
        self.assertEqual(root.findtext(".//p:GrpHdr/p:NbOfTxs", namespaces=NS), "2")
        self.assertEqual(root.findtext(".//p:GrpHdr/p:CtrlSum", namespaces=NS), "171.50")
        # Each collection is credited on its own, so the bank matcher sees
        # one movement per invoice with its reference
        payment_infos = root.findall(".//p:PmtInf", NS)
        self.assertEqual(len(payment_infos), 2)
        for info in payment_infos:
            self.assertEqual(info.findtext("p:BtchBookg", namespaces=NS), "false")
            self.assertEqual(info.findtext(".//p:LclInstrm/p:Cd", namespaces=NS), "CORE")
            self.assertEqual(info.findtext("p:ReqdColltnDt", namespaces=NS), "2026-11-02")
            self.assertEqual(info.findtext(".//p:CdtrSchmeId//p:Othr/p:Id", namespaces=NS), "ES12000B12345678")
        transaction = payment_infos[0].find("p:DrctDbtTxInf", NS)
        self.assertEqual(transaction.findtext(".//p:EndToEndId", namespaces=NS), first.payment_reference)
        self.assertEqual(transaction.findtext("p:InstdAmt", namespaces=NS), "121.00")
        self.assertEqual(transaction.findtext(".//p:MndtId", namespaces=NS), "MANDATO-001")
        self.assertEqual(transaction.findtext(".//p:DtOfSgntr", namespaces=NS), "2025-03-01")
        self.assertEqual(transaction.findtext(".//p:DbtrAcct//p:IBAN", namespaces=NS), DEBTOR_IBAN)
        self.assertIn(first.payment_reference, transaction.findtext(".//p:Ustrd", namespaces=NS))

    def test_b2b_scheme_is_written_as_the_local_instrument(self):
        xml, _skipped = self._build([self._invoice()], scheme=sepa.SepaScheme.B2B)
        root = ET.fromstring(xml)
        self.assertEqual(root.findtext(".//p:PmtInf//p:LclInstrm/p:Cd", namespaces=NS), "B2B")

    def test_collects_only_what_is_left_to_pay(self):
        invoice = self._invoice("121.00")
        with mock.patch.object(SalesInvoice, "amount_remaining", Money("21.00", "EUR")):
            xml, _skipped = self._build([invoice])
        self.assertEqual(ET.fromstring(xml).findtext(".//p:InstdAmt", namespaces=NS), "21.00")

    def test_invoices_that_cannot_be_collected_are_skipped_with_the_reason(self):
        good = self._invoice(number="F-1")
        proforma = self._invoice(number="", status=SalesInvoiceStatus.PROFORMA)
        dollars = self._invoice(number="F-2", currency="USD")
        no_mandate = self._invoice(number="F-3", client=make_client(company=self.company, name="Sin mandato"))
        bad_iban_client = make_client(company=self.company, name="IBAN malo")
        self._account(bad_iban_client, iban="1234")
        bad_iban = self._invoice(number="F-4", client=bad_iban_client)
        other_company = make_company(name="Other")
        foreign = SalesInvoice.objects.create(
            company=other_company, client=make_client(company=other_company), invoice_number="X-1",
            status=SalesInvoiceStatus.FINAL, total_amount=Money("10.00", "EUR"),
        )

        xml, skipped = self._build([good, proforma, dollars, no_mandate, bad_iban, foreign])

        self.assertEqual(ET.fromstring(xml).findtext(".//p:GrpHdr/p:NbOfTxs", namespaces=NS), "1")
        self.assertEqual(
            {entry.invoice.pk for entry in skipped},
            {proforma.pk, dollars.pk, no_mandate.pk, bad_iban.pk, foreign.pk},
        )
        self.assertTrue(all(entry.reason for entry in skipped))

    def test_paid_invoices_are_skipped(self):
        invoice = self._invoice()
        with mock.patch.object(SalesInvoice, "amount_remaining", Money("0.00", "EUR")):
            account, reason = sepa.check_invoice(invoice, self.company)
        self.assertEqual(account, self.mandated)
        self.assertTrue(reason)

    def test_prefers_the_clients_default_mandated_account(self):
        self.mandated.is_default = False
        self.mandated.save()
        default = self._account(self.client_obj, iban="DE89370400440532013000", is_default=True)
        # An account without a mandate is never charged, even if default
        ClientBankAccount.objects.create(
            company=self.company, client=self.client_obj, bank_name="Otro", iban="FR1420041010050500013M02606",
        )
        account, reason = sepa.check_invoice(self._invoice(), self.company)
        self.assertIsNone(reason)
        self.assertEqual(account, default)

    def test_creditor_identifier_is_required(self):
        self.company.sepa_creditor_id = ""
        self.company.save()
        self.creditor_account.refresh_from_db()
        with self.assertRaises(ValidationError):
            self._build([self._invoice()])

    def test_creditor_bic_is_required(self):
        self.creditor_account.swift_bic = ""
        with self.assertRaises(ValidationError):
            self._build([self._invoice()])

    def test_nothing_to_collect_is_an_error(self):
        with self.assertRaises(ValidationError):
            self._build([self._invoice(number="", status=SalesInvoiceStatus.PROFORMA)])


class SepaDirectDebitAdminTests(SepaFixtureMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.client.force_login(get_user_model().objects.create_superuser("sepa", "s@x.com", "pw"))
        self.url = reverse("admin:invoices_salesinvoice_sepa_direct_debit")

    def test_action_opens_the_page_with_the_selection(self):
        invoice = self._invoice()
        response = self.client.post(
            reverse("admin:invoices_salesinvoice_changelist"),
            {"action": "generate_sepa_file", "_selected_action": [invoice.pk]},
        )
        self.assertRedirects(response, f"{self.url}?ids={invoice.pk}", fetch_redirect_response=False)

    def test_page_lists_collected_and_skipped_invoices(self):
        good = self._invoice(number="F-1")
        skipped = self._invoice(number="F-2", client=make_client(company=self.company, name="Sin mandato"))
        response = self.client.get(f"{self.url}?ids={good.pk},{skipped.pk}")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, DEBTOR_IBAN)
        self.assertContains(response, "F-2")

    def test_post_downloads_the_file(self):
        invoice = self._invoice()
        collection_date = date.today() + timedelta(days=10)
        response = self.client.post(
            f"{self.url}?ids={invoice.pk}",
            {
                "creditor_account": self.creditor_account.pk,
                "scheme": "CORE",
                "collection_date": collection_date.strftime("%d/%m/%Y"),
            },
        )
        self.assertEqual(response.status_code, 200, getattr(response, "context", None) and response.context["form"].errors)
        self.assertEqual(response["Content-Type"], "application/xml")
        self.assertIn(f"SEPA_CORE_{collection_date:%Y%m%d}.xml", response["Content-Disposition"])
        self.assertIn(invoice.payment_reference.encode(), response.content)

    def test_post_downloads_a_b2b_file(self):
        invoice = self._invoice()
        collection_date = date.today() + timedelta(days=10)
        response = self.client.post(
            f"{self.url}?ids={invoice.pk}",
            {
                "creditor_account": self.creditor_account.pk,
                "scheme": "B2B",
                "collection_date": collection_date.strftime("%d/%m/%Y"),
            },
        )
        self.assertEqual(response.status_code, 200, getattr(response, "context", None) and response.context["form"].errors)
        self.assertIn(f"SEPA_B2B_{collection_date:%Y%m%d}.xml", response["Content-Disposition"])
        self.assertIn(b"<Cd>B2B</Cd>", response.content)

    def test_collection_date_too_soon_is_rejected(self):
        invoice = self._invoice()
        response = self.client.post(
            f"{self.url}?ids={invoice.pk}",
            {"creditor_account": self.creditor_account.pk, "scheme": "CORE", "collection_date": date.today().strftime("%d/%m/%Y")},
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["form"].errors)

    def test_invoices_of_two_companies_are_refused(self):
        other_company = make_company(name="Other")
        foreign = SalesInvoice.objects.create(
            company=other_company, client=make_client(company=other_company), invoice_number="X-1",
            status=SalesInvoiceStatus.FINAL, total_amount=Money("10.00", "EUR"),
        )
        # Both companies active in the switcher, so both invoices are listed
        cache.clear()  # switchable companies are cached per user
        session = self.client.session
        session[ActiveCompany.SESSION_KEY] = [self.company.pk, other_company.pk]
        session[ActiveCompany.SESSION_SET_AT_KEY] = time.time()
        session.save()
        response = self.client.get(f"{self.url}?ids={self._invoice().pk},{foreign.pk}")
        self.assertRedirects(response, reverse("admin:invoices_salesinvoice_changelist"), fetch_redirect_response=False)
