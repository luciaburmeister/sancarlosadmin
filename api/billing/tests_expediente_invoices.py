"""
Tests for the project-file invoice creation API
(POST /api/project-files/<id>/invoices/).
"""
import json
from datetime import date
from decimal import Decimal

from django.contrib.auth.models import User
from django.contrib.contenttypes.models import ContentType
from django.test import TestCase
from rest_framework.test import APIClient

from company.models import Company, Employee
from configurator.models import Configuration, ConfigurationStatus, ProductTemplate
from invoices.models import AppliedTax, SalesInvoice, TaxType
from management.models import ProjectFile
from sales.models import Client, Quote, QuoteInventoryItem, QuoteStatus


class _ProjectFileInvoiceTestBase(TestCase):
    """Shared fixtures for all project-file invoice API tests."""

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user("tester", password="pass")
        cls.company = Company.objects.create(name="Test Co")
        Employee.objects.create(
            user=cls.user,
            company=cls.company,
            first_name="Test",
            last_name="User",
            is_active=True,
            hired_at=date(2020, 1, 1),
        )
        cls.client_obj = Client.objects.create(name="Acme", company=cls.company)
        cls.product = ProductTemplate.objects.create(name="Door", company=cls.company)
        cls.project_file = ProjectFile.objects.create(
            name="PF Test",
            company=cls.company,
            client=cls.client_obj,
        )
        cls.iva21, _ = TaxType.objects.get_or_create(
            tax_name="IVA", country="ES", defaults={"behavior": "add"}
        )
        cls.iva10, _ = TaxType.objects.get_or_create(
            tax_name="IVA Reducido", country="ES", defaults={"behavior": "add"}
        )

    def setUp(self):
        self.api = APIClient()
        self.api.force_authenticate(user=self.user)

    # ── helpers ──────────────────────────────────────────────────────────
    def _create_config(self, quote, subtotal, total, name="Config"):
        return Configuration.objects.create(
            name=name,
            company=self.company,
            product=self.product,
            client=self.client_obj,
            quote=quote,
            project_file=self.project_file,
            status=ConfigurationStatus.COMPLETED,
            subtotal_price=subtotal,
            total_price=total,
        )

    def _attach_tax(self, obj, tax_type, pct, base=None, calc=None):
        ct = ContentType.objects.get_for_model(obj)
        return AppliedTax.objects.create(
            content_type=ct,
            object_id=obj.pk,
            tax_type=tax_type,
            selected_percentage=pct,
            base_amount=base or Decimal("0"),
            calculated_amount=calc or Decimal("0"),
        )

    def _create_quote(self, subtotal, total, tax_amount=None):
        if tax_amount is None:
            tax_amount = total - subtotal
        return Quote.objects.create(
            project_name="Q Test",
            company=self.company,
            client=self.client_obj,
            project_file=self.project_file,
            status=QuoteStatus.ACCEPTED,
            subtotal_price=subtotal,
            tax_amount=tax_amount,
            total_price=total,
        )

    def _post_create(self, pf_id=None, **overrides):
        pf_id = pf_id or self.project_file.pk
        payload = {
            "invoice_mode": "percentage",
            "percentage": 100,
            **overrides,
        }
        return self.api.post(
            f"/es/api/project-files/{pf_id}/invoices/",
            data=json.dumps(payload),
            content_type="application/json",
        )


class ProjectFileInvoiceCreateTests(_ProjectFileInvoiceTestBase):
    """Basic create-invoice happy path and error handling."""

    def test_create_returns_201(self):
        """Invoicing a project file with one config returns 201."""
        quote = self._create_quote(subtotal=100, total=121)
        config = self._create_config(quote, subtotal=100, total=121)
        self._attach_tax(config, self.iva21, 21, base=100, calc=21)

        resp = self._post_create()
        self.assertEqual(resp.status_code, 201, resp.json())

        data = resp.json()
        self.assertIn("id", data)
        self.assertEqual(data["status"], "proforma")
        self.assertIsNone(data["invoice_number"])
        self.assertNotIn("email_queued", data)
        self.assertGreater(data["subtotal_amount"], 0)
        self.assertGreater(data["total_amount"], data["subtotal_amount"])

    def test_no_accepted_quotes_returns_400(self):
        """A project file with no accepted quotes should be rejected."""
        resp = self._post_create()
        self.assertEqual(resp.status_code, 400)
        self.assertIn("error", resp.json())

    def test_nonexistent_project_file_returns_404(self):
        resp = self._post_create(pf_id=99999)
        self.assertEqual(resp.status_code, 404)


class ProjectFileInvoiceInventoryItemTests(_ProjectFileInvoiceTestBase):
    """Inventory items should appear as line items and be taxed correctly."""

    def _attach_inv_item_tax_and_recalc(self, qi, tax_type, pct, base, calc):
        """Attach tax to a QuoteInventoryItem and trigger recalculation.

        In production, taxes are copied during QII.save() via _copy_taxes_from_item.
        In tests we attach them manually, so we must trigger recalc afterwards.
        """
        self._attach_tax(qi, tax_type, pct, base=base, calc=calc)
        qi._recalc_total_with_tax()
        qi.quote.recalc_totals()

    def test_inventory_items_included_and_taxed(self):
        """QuoteInventoryItems show up as separate line items on the invoice
        and their taxes are part of the invoice tax amount."""
        from inventory.models import InventoryItem

        quote = self._create_quote(subtotal=100, total=121)
        config = self._create_config(quote, subtotal=100, total=121, name="Door Config")
        self._attach_tax(config, self.iva21, 21, base=100, calc=21)

        inv_item = InventoryItem.objects.create(
            name="Steel Tube",
            company=self.company,
            selling_price=50,
        )
        qi = QuoteInventoryItem.objects.create(
            quote=quote,
            inventory_item=inv_item,
            quantity=1,
            unit_price=50,
            discount=0,
        )
        self._attach_inv_item_tax_and_recalc(qi, self.iva21, 21, base=50, calc=Decimal("10.50"))

        resp = self._post_create()
        self.assertEqual(resp.status_code, 201, resp.json())

        data = resp.json()
        descriptions = [i["description"] for i in data["items"]]
        self.assertTrue(
            any("Steel Tube" in d for d in descriptions),
            f"Inventory item not found in line items: {descriptions}",
        )

        subtotal = Decimal(str(data["subtotal_amount"]))
        tax = Decimal(str(data["tax_amount"]))
        total = Decimal(str(data["total_amount"]))

        expected_tax = (subtotal * Decimal("21") / Decimal("100")).quantize(Decimal("0.01"))
        self.assertEqual(tax, expected_tax, f"Tax mismatch: {tax} != {expected_tax}")
        self.assertEqual(total, subtotal + tax)


class ProjectFileInvoiceMixedTaxTests(_ProjectFileInvoiceTestBase):
    """Per-item tax computation should handle different tax rates."""

    def test_mixed_tax_rates(self):
        """Configs at 21% and inventory items at 10% produce separate tax entries."""
        from inventory.models import InventoryItem

        quote = self._create_quote(subtotal=100, total=121)
        config = self._create_config(quote, subtotal=100, total=121, name="Door 21%")
        self._attach_tax(config, self.iva21, 21, base=100, calc=21)

        inv_item = InventoryItem.objects.create(
            name="Reduced Item", company=self.company, selling_price=100
        )
        qi = QuoteInventoryItem.objects.create(
            quote=quote,
            inventory_item=inv_item,
            quantity=1,
            unit_price=100,
            discount=0,
        )
        # 10% reduced rate — attach and recalc so quote total is correct
        self._attach_tax(qi, self.iva10, 10, base=100, calc=10)
        qi._recalc_total_with_tax()
        qi.quote.recalc_totals()

        resp = self._post_create()
        self.assertEqual(resp.status_code, 201, resp.json())

        invoice = SalesInvoice.objects.get(pk=resp.json()["id"])
        ct = ContentType.objects.get_for_model(invoice)
        taxes = list(
            AppliedTax.objects.filter(content_type=ct, object_id=invoice.pk)
            .order_by("selected_percentage")
        )

        self.assertEqual(len(taxes), 2, "Expected two separate tax entries for mixed rates")

        # 10% entry
        self.assertEqual(taxes[0].selected_percentage, Decimal("10"))
        self.assertEqual(taxes[0].calculated_amount.amount, Decimal("10.00"))

        # 21% entry
        self.assertEqual(taxes[1].selected_percentage, Decimal("21"))
        self.assertEqual(taxes[1].calculated_amount.amount, Decimal("21.00"))

        # Total
        self.assertEqual(
            invoice.tax_amount.amount,
            Decimal("31.00"),
            "Total tax should be 21 + 10 = 31",
        )


class ProjectFileInvoicePartialTests(_ProjectFileInvoiceTestBase):
    """Partial invoicing should work correctly via the API."""

    def test_50_percent_invoice(self):
        """Invoicing 50% creates an invoice at half the subtotal."""
        quote = self._create_quote(subtotal=200, total=242)
        config = self._create_config(quote, subtotal=200, total=242)
        self._attach_tax(config, self.iva21, 21, base=200, calc=42)

        resp = self._post_create(percentage=50)
        self.assertEqual(resp.status_code, 201, resp.json())

        data = resp.json()
        subtotal = Decimal(str(data["subtotal_amount"]))
        self.assertEqual(subtotal, Decimal("100.00"))

    def test_custom_amount(self):
        """Invoicing a custom amount works."""
        quote = self._create_quote(subtotal=1000, total=1210)
        config = self._create_config(quote, subtotal=1000, total=1210)
        self._attach_tax(config, self.iva21, 21, base=1000, calc=210)

        resp = self._post_create(invoice_mode="custom", custom_amount=500)
        self.assertEqual(resp.status_code, 201, resp.json())

        data = resp.json()
        subtotal = Decimal(str(data["subtotal_amount"]))
        self.assertEqual(subtotal, Decimal("500.00"))



class BulkEmitQuoteObservationsTests(_ProjectFileInvoiceTestBase):
    """Bulk emit should copy quote observations into invoice notes when enabled."""

    def _setup_invoiceable_pf(self, observations="Cambio de aceite incluido"):
        quote = self._create_quote(subtotal=100, total=121)
        quote.observations = observations
        quote.save()
        config = self._create_config(quote, subtotal=100, total=121)
        self._attach_tax(config, self.iva21, 21, base=100, calc=21)
        return quote

    def _post_bulk(self, **overrides):
        payload = {
            "project_file_ids": [self.project_file.pk],
            "send_email": False,
            **overrides,
        }
        return self.api.post(
            "/es/api/sales-invoices/bulk-emit/",
            data=json.dumps(payload),
            content_type="application/json",
        )

    def _created_invoice(self, resp):
        self.assertEqual(resp.status_code, 200, resp.json())
        data = resp.json()
        self.assertEqual(data["total_created"], 1, data)
        return SalesInvoice.objects.get(pk=data["successes"][0]["invoice_id"])

    def test_flag_off_keeps_common_notes_only(self):
        self._setup_invoiceable_pf()
        invoice = self._created_invoice(self._post_bulk(notes="Nota común"))
        self.assertEqual(invoice.notes, "Nota común")

    def test_company_flag_without_common_notes(self):
        self.company.bulk_emit_copy_quote_observations = True
        self.company.save()
        self._setup_invoiceable_pf()
        invoice = self._created_invoice(self._post_bulk())
        self.assertEqual(invoice.notes, "Cambio de aceite incluido")

    def test_request_param_overrides_company_flag_off(self):
        self.company.bulk_emit_copy_quote_observations = True
        self.company.save()
        self._setup_invoiceable_pf()
        invoice = self._created_invoice(
            self._post_bulk(notes="Nota común", include_quote_observations=False)
        )
        self.assertEqual(invoice.notes, "Nota común")

    def test_request_param_overrides_company_flag_on(self):
        self._setup_invoiceable_pf()
        invoice = self._created_invoice(
            self._post_bulk(include_quote_observations=True)
        )
        self.assertEqual(invoice.notes, "Cambio de aceite incluido")

    def test_auto_accepted_quote_observations_included(self):
        """Draft quotes auto-accepted during bulk emit contribute their observations."""
        self.company.bulk_emit_copy_quote_observations = True
        self.company.save()
        quote = self._create_quote(subtotal=100, total=121)
        quote.status = QuoteStatus.DRAFT
        quote.observations = "Observación de borrador"
        quote.save()
        config = self._create_config(quote, subtotal=100, total=121)
        self._attach_tax(config, self.iva21, 21, base=100, calc=21)

        invoice = self._created_invoice(self._post_bulk())
        self.assertEqual(invoice.notes, "Observación de borrador")

    def test_multiple_quotes_observations_joined(self):
        self.company.bulk_emit_copy_quote_observations = True
        self.company.save()
        self._setup_invoiceable_pf(observations="Obs A")
        quote2 = self._create_quote(subtotal=50, total=60.50)
        quote2.observations = "  Obs B  "
        quote2.save()
        config2 = self._create_config(quote2, subtotal=50, total=60.50, name="Config 2")
        self._attach_tax(config2, self.iva21, 21, base=50, calc=10.50)

        invoice = self._created_invoice(self._post_bulk(notes="Común"))
        self.assertEqual(invoice.notes, "Común\n---\nObs A\n---\nObs B")

    def test_blank_observations_skipped(self):
        self.company.bulk_emit_copy_quote_observations = True
        self.company.save()
        self._setup_invoiceable_pf(observations="   ")
        invoice = self._created_invoice(self._post_bulk(notes="Nota común"))
        self.assertEqual(invoice.notes, "Nota común")

    def test_candidates_endpoint_exposes_company_default(self):
        resp = self.api.get("/es/api/sales-invoices/bulk-emit-candidates/")
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.json()["include_quote_observations_default"])

        self.company.bulk_emit_copy_quote_observations = True
        self.company.save()
        resp = self.api.get("/es/api/sales-invoices/bulk-emit-candidates/")
        self.assertTrue(resp.json()["include_quote_observations_default"])


class ProjectFileInvoiceDirectFinalizeTests(_ProjectFileInvoiceTestBase):
    """Creating an invoice with finalize=true must emit the final invoice directly."""

    def _setup_invoiceable_pf(self):
        quote = self._create_quote(subtotal=100, total=121)
        config = self._create_config(quote, subtotal=100, total=121)
        self._attach_tax(config, self.iva21, 21, base=100, calc=21)

    def test_finalize_true_creates_final_invoice_without_sending_email(self):
        """Emails stay a separate user action, as in the legacy flow: even
        with a client address on file nothing is queued unless asked."""
        self._setup_invoiceable_pf()
        self.client_obj.email = "cliente@example.com"
        self.client_obj.save()

        resp = self._post_create(finalize=True)
        self.assertEqual(resp.status_code, 201, resp.json())

        data = resp.json()
        self.assertEqual(data["status"], "final")
        self.assertIsNotNone(data["invoice_number"])
        self.assertIn("email_queued", data)
        self.assertFalse(data["email_queued"])
        self.assertEqual(data["email_skip_reason"], "disabled")

        invoice = SalesInvoice.objects.get(pk=data["id"])
        self.assertIsNotNone(invoice.incremental_id)

    def test_existing_proforma_can_still_be_finalized(self):
        """The legacy proforma → finalize flow must keep working."""
        self._setup_invoiceable_pf()
        created = self._post_create().json()
        self.assertEqual(created["status"], "proforma")

        resp = self.api.post(
            f"/es/api/sales-invoices/{created['id']}/finalize/",
            data=json.dumps({"send_email": False}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200, resp.json())
        self.assertEqual(resp.json()["status"], "final")
        self.assertIsNotNone(resp.json()["invoice_number"])

    def test_direct_finalize_gets_sequential_number(self):
        """Direct-final invoices share the same sequence as legacy finalizes."""
        self._setup_invoiceable_pf()
        first = self._post_create(percentage=50, finalize=True, send_email=False).json()
        second = self._post_create(percentage=100, finalize=True, send_email=False).json()

        first_inv = SalesInvoice.objects.get(pk=first["id"])
        second_inv = SalesInvoice.objects.get(pk=second["id"])
        self.assertEqual(second_inv.incremental_id, first_inv.incremental_id + 1)
