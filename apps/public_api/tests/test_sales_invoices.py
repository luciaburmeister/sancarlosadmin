"""/api/v1/sales-invoices/: list, detail and draft creation."""
from datetime import date
from decimal import Decimal

from django.contrib.contenttypes.models import ContentType
from django.test import TestCase
from djmoney.money import Money
from rest_framework.test import APIClient

from apps.public_api.models import IdempotencyRecord
from apps.public_api.tests.factories import auth_header, make_api_key, make_api_user
from invoices.models import SalesInvoice, SalesInvoiceItem, SalesInvoiceStatus, TaxType
from invoices.services.manual_sales_invoice import DraftLine, LineTax, create_draft_sales_invoice
from invoices.tests.digest_fixtures import make_sales_invoice
from invoices.tests.factories import apply_tax, get_irpf, get_iva21, make_client, make_company
from payments.models import Payment, PaymentAllocation

LIST_URL = "/api/v1/sales-invoices/"


def detail_url(pk):
    return f"/api/v1/sales-invoices/{pk}/"


class _Base(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = make_company()
        cls.other_company = make_company(name="Other Co")
        cls.client_obj = make_client(company=cls.company, name="Acme")
        cls.user = make_api_user(
            company=cls.company, perms=["invoices.view_salesinvoice", "invoices.add_salesinvoice"],
        )
        get_iva21()
        get_irpf()

    def setUp(self):
        self.api = APIClient()
        _, secret = make_api_key(user=self.user, company=self.company)
        self.headers = auth_header(secret)

    def get(self, url, **params):
        return self.api.get(url, params, **self.headers)

    def post(self, data, **headers):
        return self.api.post(LIST_URL, data, format="json", **self.headers, **headers)


class ListTests(_Base):
    def test_lists_only_the_key_company_newest_first_with_money_as_strings(self):
        older = make_sales_invoice(company=self.company, number="F-1", total="121.00", date_issued=date(2026, 1, 1))
        newer = make_sales_invoice(company=self.company, number="F-2", total="50.5", date_issued=date(2026, 2, 1))
        make_sales_invoice(company=self.other_company, number="X-1")

        body = self.get(LIST_URL).json()

        self.assertEqual(body["count"], 2)
        self.assertEqual([row["id"] for row in body["results"]], [newer.pk, older.pk])
        row = body["results"][1]
        self.assertEqual(row["invoice_number"], "F-1")
        self.assertEqual(row["total"], "121.00")
        self.assertEqual(row["amount_paid"], "0.00")
        self.assertEqual(row["amount_remaining"], "121.00")
        self.assertEqual(row["currency"], "EUR")
        self.assertEqual(row["client"]["name"], "Cliente F-1")

    def test_filters(self):
        make_sales_invoice(company=self.company, number="F-1", date_issued=date(2026, 1, 10))
        make_sales_invoice(company=self.company, number="F-2", date_issued=date(2026, 3, 10), client=self.client_obj)
        make_sales_invoice(
            company=self.company, number="P-1", status=SalesInvoiceStatus.PROFORMA, date_issued=date(2026, 3, 11),
        )

        def numbers(**params):
            return [row["invoice_number"] for row in self.get(LIST_URL, **params).json()["results"]]

        self.assertEqual(numbers(status="proforma"), ["P-1"])
        self.assertEqual(numbers(client_id=self.client_obj.pk), ["F-2"])
        self.assertEqual(numbers(date_from="2026-03-01", date_to="2026-03-10"), ["F-2"])
        self.assertEqual(numbers(search="acme"), ["F-2"])

    def test_invalid_filter_is_400_with_details(self):
        response = self.get(LIST_URL, status="bogus", date_from="10/01/2026")
        self.assertEqual(response.status_code, 400)
        error = response.json()["error"]
        self.assertEqual(error["code"], "invalid")
        self.assertEqual(set(error["details"]), {"status", "date_from"})

    def test_paginates(self):
        for n in range(3):
            make_sales_invoice(company=self.company, number=f"F-{n}")
        body = self.get(LIST_URL, page_size=2).json()
        self.assertEqual(body["count"], 3)
        self.assertEqual(len(body["results"]), 2)
        self.assertIsNotNone(body["next"])

    def test_amount_paid_from_allocations(self):
        invoice = make_sales_invoice(company=self.company, number="F-1", total="100.00")
        self._pay(invoice, "40.00")
        row = self.get(LIST_URL).json()["results"][0]
        self.assertEqual(row["amount_paid"], "40.00")
        self.assertEqual(row["amount_remaining"], "60.00")

    def _pay(self, invoice, amount):
        payment = Payment.objects.create(
            company=self.company, payment_date=date(2026, 5, 1), amount=Money(amount, "EUR"),
        )
        PaymentAllocation.objects.create(
            payment=payment, content_type=ContentType.objects.get_for_model(invoice),
            object_id=invoice.pk, amount_applied=Money(amount, "EUR"),
        )


class DetailTests(_Base):
    def test_detail_with_lines_and_taxes(self):
        invoice = make_sales_invoice(
            company=self.company, number="F-1", status=SalesInvoiceStatus.PROFORMA, client=self.client_obj,
        )
        SalesInvoiceItem.objects.create(invoice=invoice, description="Door", quantity=2, unit_price=Decimal("50"))
        apply_tax(invoice, tax_type=get_iva21(), percentage="21", base="100.00")

        body = self.get(detail_url(invoice.pk)).json()

        self.assertEqual(body["lines"][0]["description"], "Door")
        self.assertEqual(body["lines"][0]["line_total"], "100.00")
        self.assertEqual(
            body["taxes"],
            [{
                "tax_type_id": get_iva21().pk, "name": "IVA", "rate": "21.00", "behavior": "add",
                "base": "100.00", "amount": "21.00",
            }],
        )
        self.assertEqual(body["payments"], [])

    def test_other_company_invoice_is_404(self):
        invoice = make_sales_invoice(company=self.other_company, number="X-1")
        response = self.get(detail_url(invoice.pk))
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["error"]["code"], "not_found")


class CreateDraftTests(_Base):
    def tax(self, tax_type, rate):
        return {"tax_type_id": tax_type.pk, "rate": rate}

    def payload(self, **overrides):
        iva, irpf = get_iva21(), get_irpf()
        data = {
            "client_id": self.client_obj.pk,
            "date_issued": "2026-11-04",
            "reference": "ORDER-77",
            "lines": [
                {
                    "description": "Window", "quantity": "2", "unit_price": "100.00",
                    "taxes": [self.tax(iva, "21"), self.tax(irpf, "15")],
                },
                {
                    "description": "Repair", "quantity": "1", "unit_price": "50.00",
                    "taxes": [self.tax(iva, "10"), self.tax(irpf, "15")],
                },
                {"description": "Untaxed fee", "quantity": "1", "unit_price": "5.00"},
            ],
        }
        data.update(overrides)
        return data

    def test_creates_a_proforma_with_one_tax_per_type_and_rate(self):
        response = self.post(self.payload())

        self.assertEqual(response.status_code, 201, response.content)
        body = response.json()
        self.assertEqual(response["Location"], detail_url(body["id"]))
        self.assertEqual(body["status"], "proforma")
        self.assertIsNone(body["invoice_number"])
        self.assertTrue(body["proforma_number"])
        self.assertEqual(body["reference"], "ORDER-77")
        self.assertEqual(body["subtotal"], "255.00")
        # VAT 42.00 + 5.00; retention 15% of the 250.00 that carries it = 37.50
        self.assertEqual(body["tax"], "9.50")
        self.assertEqual(body["total"], "264.50")
        self.assertEqual(
            [(t["name"], t["rate"], t["base"], t["amount"]) for t in body["taxes"]],
            [("IVA", "21.00", "200.00", "42.00"), ("IRPF", "15.00", "250.00", "37.50"), ("IVA", "10.00", "50.00", "5.00")],
        )
        invoice = SalesInvoice.objects.get(pk=body["id"])
        self.assertEqual(invoice.company, self.company)
        self.assertEqual(invoice.items.count(), 3)

    def test_rate_outside_the_tax_type_options_is_400(self):
        restricted = TaxType.objects.create(
            tax_name="VAT", country="PT", behavior="add", percentage_options=[6, 13, 23],
        )
        line = {"description": "X", "quantity": "1", "unit_price": "10", "taxes": [self.tax(restricted, "21")]}
        response = self.post(self.payload(lines=[line]))
        self.assertEqual(response.status_code, 400)
        self.assertIn("lines", response.json()["error"]["details"])
        self.assertFalse(SalesInvoice.objects.exists())

    def test_rate_within_the_tax_type_options_is_accepted(self):
        restricted = TaxType.objects.create(
            tax_name="VAT", country="PT", behavior="add", percentage_options=[6, 13, 23],
        )
        line = {"description": "X", "quantity": "1", "unit_price": "100", "taxes": [self.tax(restricted, "23")]}
        response = self.post(self.payload(lines=[line]))
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(response.json()["total"], "123.00")

    def test_inactive_or_unknown_tax_type_is_400(self):
        inactive = TaxType.objects.create(tax_name="Old", country="ES", behavior="add", is_active=False)
        for tax_type_id in (inactive.pk, 999_999):
            line = {"description": "X", "quantity": "1", "unit_price": "10", "taxes": [{"tax_type_id": tax_type_id, "rate": "5"}]}
            response = self.post(self.payload(lines=[line]))
            self.assertEqual(response.status_code, 400, tax_type_id)

    def test_same_tax_type_twice_on_a_line_is_400(self):
        iva = get_iva21()
        line = {"description": "X", "quantity": "1", "unit_price": "10", "taxes": [self.tax(iva, "21"), self.tax(iva, "10")]}
        response = self.post(self.payload(lines=[line]))
        self.assertEqual(response.status_code, 400)

    def test_client_of_another_company_is_400(self):
        foreign = make_client(company=self.other_company, name="Foreign")
        response = self.post(self.payload(client_id=foreign.pk))
        self.assertEqual(response.status_code, 400)
        self.assertIn("client_id", response.json()["error"]["details"])
        self.assertFalse(SalesInvoice.objects.exists())

    def test_invalid_body_is_400(self):
        response = self.post(self.payload(lines=[], due_date="2026-01-01"))
        self.assertEqual(response.status_code, 400)
        self.assertIn("lines", response.json()["error"]["details"])

    def test_same_idempotency_key_replays_the_first_response(self):
        first = self.post(self.payload(), HTTP_IDEMPOTENCY_KEY="abc-1")
        again = self.post(self.payload(), HTTP_IDEMPOTENCY_KEY="abc-1")

        self.assertEqual(again.status_code, 201, again.content)
        self.assertEqual(again.json()["id"], first.json()["id"])
        self.assertEqual(again["Idempotent-Replayed"], "true")
        self.assertEqual(SalesInvoice.objects.count(), 1)

    def test_same_idempotency_key_with_another_body_is_409(self):
        self.post(self.payload(), HTTP_IDEMPOTENCY_KEY="abc-1")
        response = self.post(self.payload(reference="OTHER"), HTTP_IDEMPOTENCY_KEY="abc-1")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"]["code"], "idempotency_key_reused")

    def test_failed_request_keeps_no_idempotency_record(self):
        self.post(self.payload(lines=[]), HTTP_IDEMPOTENCY_KEY="abc-1")
        self.assertFalse(IdempotencyRecord.objects.exists())
        self.assertEqual(self.post(self.payload(), HTTP_IDEMPOTENCY_KEY="abc-1").status_code, 201)


class SchemaTests(_Base):
    def test_schema_documents_only_the_public_api(self):
        response = self.api.get("/api/v1/schema/", {"format": "json"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            set(response.json()["paths"]),
            {
                "/api/v1/clients/", "/api/v1/clients/{client_id}/",
                "/api/v1/sales-invoices/", "/api/v1/sales-invoices/{invoice_id}/",
                "/api/v1/sales-invoices/{invoice_id}/finalize/",
                "/api/v1/tax-types/",
            },
        )


class FinalizeTests(_Base):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.finalizer = make_api_user(
            company=cls.company, username="finalizer",
            perms=["invoices.view_salesinvoice", "invoices.change_salesinvoice"],
        )

    def setUp(self):
        super().setUp()
        _, secret = make_api_key(user=self.finalizer, company=self.company)
        self.headers = auth_header(secret)

    def draft(self, company=None, client=None):
        line = DraftLine(
            description="Window", quantity=Decimal("1"), unit_price=Decimal("100.00"),
            taxes=(LineTax(get_iva21(), Decimal("21")),),
        )
        company = company or self.company
        client = client or (self.client_obj if company == self.company else make_client(company=company))
        return create_draft_sales_invoice(company=company, client=client, lines=[line])

    def finalize(self, invoice_id, data=None, **headers):
        return self.api.post(
            f"{detail_url(invoice_id)}finalize/", data or {}, format="json", **self.headers, **headers,
        )

    def test_issues_the_proforma_without_emailing_by_default(self):
        draft = self.draft()

        response = self.finalize(draft.pk)

        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        self.assertEqual(body["status"], "final")
        self.assertTrue(body["invoice_number"])
        self.assertEqual(body["proforma_number"], draft.proforma_number)
        self.assertEqual(body["total"], "121.00")
        self.assertEqual(body["email"], {"queued": False, "to": None, "skip_reason": "disabled"})
        draft.refresh_from_db()
        self.assertEqual(draft.status, SalesInvoiceStatus.FINAL)

    def test_send_email_queues_it_to_the_client(self):
        self.client_obj.email = "billing@acme.test"
        self.client_obj.save()
        response = self.finalize(self.draft().pk, {"send_email": True})
        self.assertEqual(
            response.json()["email"], {"queued": True, "to": "billing@acme.test", "skip_reason": None},
        )

    def test_send_email_without_client_email_says_why(self):
        response = self.finalize(self.draft().pk, {"send_email": True})
        self.assertEqual(response.json()["email"]["skip_reason"], "no_client_email")

    def test_already_final_is_409(self):
        draft = self.draft()
        self.finalize(draft.pk)
        response = self.finalize(draft.pk)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"]["code"], "invoice_not_proforma")

    def test_same_idempotency_key_replays_instead_of_409(self):
        draft = self.draft()
        first = self.finalize(draft.pk, HTTP_IDEMPOTENCY_KEY="fin-1")
        again = self.finalize(draft.pk, HTTP_IDEMPOTENCY_KEY="fin-1")
        self.assertEqual(again.status_code, 200)
        self.assertEqual(again.json()["invoice_number"], first.json()["invoice_number"])
        self.assertEqual(again["Idempotent-Replayed"], "true")

    def test_other_company_invoice_is_404(self):
        foreign = self.draft(company=self.other_company)
        self.assertEqual(self.finalize(foreign.pk).status_code, 404)
        foreign.refresh_from_db()
        self.assertEqual(foreign.status, SalesInvoiceStatus.PROFORMA)

    def test_needs_change_permission(self):
        _, secret = make_api_key(user=self.user, company=self.company)  # view + add only
        response = self.api.post(f"{detail_url(self.draft().pk)}finalize/", {}, format="json", **auth_header(secret))
        self.assertEqual(response.status_code, 403)
