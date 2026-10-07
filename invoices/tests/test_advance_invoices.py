"""
Creating advance invoices (facturas de anticipo): AdvanceInvoiceService, the
admin wizard's advance mode and its live preview.

- the user's expediente (1,557.49 of line items, 2 % financing, IVA 21 %)
  at 30 % gives 457.90 / 96.16 / 554.06 with no discount;
- one ``advance_pf_`` line per VAT group, each with its own taxes, and the
  invoice's taxes on the groups that carry them (IRPF subtracted, RE added);
- amount mode splits the amount by largest remainder and derives the %;
- a VAT group whose goods were invoiced before the advance gets no part of
  it: the advance goes on the groups with goods left;
- invalid percentages or amounts, and advances beyond the cap (pro formas
  included), are refused; finalizing re-checks the cap, per VAT group too;
- the company is always the expediente's, and the wizard does not open
  another company's expediente;
- the wizard's setup step carries the advance mode's own labels;
- the preview is what create() issues, and writes nothing.
"""
import re
from datetime import date
from decimal import Decimal

from django.contrib.auth.models import Permission, User
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from company.models import Company, CompanyUser
from configurator.models import Configuration
from invoices.forms.project_file_wizard_forms import ProjectFileInvoiceSetupForm
from invoices.models import (
    AppliedTax,
    SalesInvoice,
    SalesInvoiceDocumentType,
    SalesInvoiceItem,
    SalesInvoiceStatus,
    TaxType,
)
from invoices.services.advances import AdvanceDeductionService, AdvanceInvoiceService
from invoices.services.discounts import is_advance_line_source, source_kind
from invoices.services.project_file_invoice_service import ProjectFileInvoiceService
from invoices.services.sales_invoice_service import SalesInvoiceService
from invoices.tests.advance_fixtures import AdvanceExpedienteMixin, UserExpedienteMixin
from management.models import ProjectFile
from materials.models import PaymentMethod
from sales.models import Client, Quote, QuoteStatus


def _create(project_file, **kwargs):
    return AdvanceInvoiceService.create(project_file, date_issued=date(2026, 9, 3), **kwargs)


def _invoice_taxes(invoice):
    ct = ContentType.objects.get_for_model(invoice)
    return [
        (tax.tax_type.tax_name, tax.selected_percentage, tax.base_amount.amount,
         tax.calculated_amount.amount, tax.order)
        for tax in AppliedTax.objects.filter(content_type=ct, object_id=invoice.pk)
        .select_related("tax_type").order_by("order", "pk")
    ]


def _line_taxes(line):
    return [
        (tax.tax_type.tax_name, tax.selected_percentage, tax.base_amount.amount,
         tax.calculated_amount.amount)
        for tax in line.applied_taxes.select_related("tax_type").order_by("order", "pk")
    ]


def _totals(invoice):
    invoice.refresh_from_db()
    return (
        invoice.subtotal_amount.amount,
        invoice.discount_amount.amount,
        invoice.taxable_base.amount,
        invoice.tax_amount.amount,
        invoice.total_amount.amount,
    )


class UserExpedienteAdvanceTests(UserExpedienteMixin, TestCase):
    """The user's expediente: 1,557.49 of line items at IVA 21 %, with a 2 %
    financing rule on those line items (accumulated)."""

    def test_fixture_is_the_users_quote(self):
        [entry] = self.quote.discount_breakdown
        self.assertEqual(Decimal(str(entry["base"])), Decimal("1557.49"))
        self.assertEqual(Decimal(str(entry["amount"])), Decimal("31.15"))
        self.assertEqual(self.quote.discounted_subtotal.amount, Decimal("1526.34"))

    def test_thirty_percent_advance(self):
        invoice = _create(self.project_file, percentage=Decimal("30"))

        # 1,526.34 × 30 % = 457.902 → 457.90; IVA 96.159 → 96.16
        self.assertEqual(
            _totals(invoice),
            (Decimal("457.90"), Decimal("0.00"), Decimal("457.90"), Decimal("96.16"), Decimal("554.06")),
        )
        self.assertEqual(invoice.discount_breakdown, [])
        self.assertEqual(invoice.document_type, SalesInvoiceDocumentType.ADVANCE)
        self.assertEqual(invoice.status, SalesInvoiceStatus.PROFORMA)
        self.assertEqual(invoice.advance_percentage, Decimal("30.0000"))
        self.assertEqual(invoice.project_file, self.project_file)
        self.assertEqual(invoice.company, self.company)
        self.assertEqual(invoice.client, self.client_obj)
        self.assertIsNone(invoice.invoice_number)
        self.assertTrue(invoice.proforma_number)

        [line] = invoice.items.all()
        self.assertEqual(line.source_id, f"advance_pf_{self.project_file.pk}")
        self.assertTrue(is_advance_line_source(line.source_id))
        self.assertIsNone(source_kind(line.source_id))
        self.assertIsNone(line.project_file)
        self.assertIsNone(line.advance_line)
        self.assertEqual((line.quantity, line.unit_price.amount), (Decimal("1.00"), Decimal("457.90")))
        self.assertEqual(
            _line_taxes(line), [("IVA", Decimal("21.00"), Decimal("457.90"), Decimal("96.16"))],
        )
        self.assertEqual(
            _invoice_taxes(invoice), [("IVA", Decimal("21.00"), Decimal("457.90"), Decimal("96.16"), 0)],
        )

    def test_the_expediente_counts_it(self):
        invoice = _create(self.project_file, percentage=Decimal("30"))

        self.project_file.refresh_from_db()
        self.assertEqual(self.project_file.advance_pending_amount.amount, Decimal("457.90"))
        summary = ProjectFileInvoiceService.get_invoiceable_summary(self.project_file)
        self.assertEqual(summary["advances_open_net"], Decimal("457.90"))
        self.assertEqual(summary["goods_remaining_net"], Decimal("1526.34"))
        [advance] = summary["advances"]
        self.assertEqual((advance["id"], advance["percentage"]), (invoice.pk, Decimal("30.0000")))

    def test_recalculating_moves_nothing(self):
        invoice = _create(self.project_file, percentage=Decimal("30"))
        before = _totals(invoice)

        invoice.recalculate_totals()
        invoice.save()

        self.assertEqual(_totals(invoice), before)

    def test_preview_is_what_create_issues(self):
        with CaptureQueriesContext(connection) as ctx:
            preview = AdvanceInvoiceService.preview(self.project_file, percentage=Decimal("30"))
        writes = [
            q["sql"] for q in ctx.captured_queries
            if re.match(r"\s*(INSERT|UPDATE|DELETE)", q["sql"], re.IGNORECASE)
        ]
        self.assertEqual(writes, [])
        self.assertFalse(SalesInvoice.objects.exists())

        invoice = _create(self.project_file, percentage=Decimal("30"))
        self.assertIsNone(preview["error"])
        self.assertEqual(preview["net_base"], Decimal("1526.34"))
        self.assertEqual(preview["max_amount"], Decimal("1526.34"))
        self.assertEqual(
            (preview["base"], preview["tax_amount"], preview["total"]),
            _totals(invoice)[2:],
        )
        [line] = invoice.items.all()
        self.assertEqual(
            [(p["description"], p["base"]) for p in preview["lines"]],
            [(line.description, line.unit_price.amount)],
        )
        self.assertEqual(
            [(t["name"], t["percentage"], t["base"], t["amount"]) for t in preview["taxes"]],
            [row[:4] for row in _invoice_taxes(invoice)],
        )


class _AdvanceTestBase(AdvanceExpedienteMixin, TestCase):
    """An expediente whose quote holds a 1,000.00 configuration at IVA 21 %."""

    def _tax_type(self, name, behavior="add"):
        tax_type, _created = TaxType.objects.get_or_create(
            tax_name=name, country="ES", defaults={"behavior": behavior},
        )
        return tax_type

    def _add_tax(self, config, tax_type, percentage):
        AppliedTax.objects.create(
            content_type=ContentType.objects.get_for_model(Configuration),
            object_id=config.pk,
            tax_type=tax_type,
            selected_percentage=percentage,
        )


class DescriptionTests(_AdvanceTestBase):
    """The advance line names the accepted quotes (number, name and date),
    not the expediente."""

    def _description(self):
        return _create(self.project_file, percentage=Decimal("30")).items.get().description

    def test_names_the_accepted_quote_with_number_and_date(self):
        Quote.objects.filter(pk=self.quote.pk).update(
            project_name="Ventanas planta baja", date=date(2026, 9, 15),
        )
        description = self._description()
        self.assertIn(f"nº {self.quote.pk} Ventanas planta baja (15/09/2026)", description)
        self.assertNotIn(self.project_file.reference, description)

    def test_lists_every_accepted_quote(self):
        Quote.objects.filter(pk=self.quote.pk).update(project_name="Fachada", date=date(2026, 9, 1))
        other = Quote.objects.create(
            project_name="Cocina", date=date(2026, 9, 10), company=self.company,
            client=self.client_obj, project_file=self.project_file, status=QuoteStatus.ACCEPTED,
        )
        self.add_config(other, Decimal("200.00"))
        Quote.objects.create(
            project_name="Rechazado", company=self.company, client=self.client_obj,
            project_file=self.project_file, status=QuoteStatus.REJECTED,
        )
        description = self._description()
        self.assertIn(
            f"nº {self.quote.pk} Fachada (01/09/2026), nº {other.pk} Cocina (10/09/2026)",
            description,
        )
        self.assertNotIn("Rechazado", description)

    def test_many_quotes_are_trimmed_to_fit_the_line(self):
        # Every accepted quote is named, lines or not, so no configurations:
        # add_config re-totals the quote and is what made this test slow. Ten
        # ~63-character labels already overflow the 440 the line leaves.
        for i in range(10):
            Quote.objects.create(
                project_name=f"Presupuesto con un nombre bastante largo {i}",
                company=self.company, client=self.client_obj,
                project_file=self.project_file, status=QuoteStatus.ACCEPTED,
            )
        description = self._description()
        self.assertLessEqual(len(description), 500)
        self.assertTrue(description.endswith(", …"))
        self.assertIn(f"nº {self.quote.pk} ", description)

    def test_quote_without_name_or_date_shows_its_number(self):
        Quote.objects.filter(pk=self.quote.pk).update(project_name="", date=None)
        self.assertTrue(self._description().endswith(f"nº {self.quote.pk}"))

    def test_falls_back_to_the_expediente_without_accepted_quotes(self):
        Quote.objects.filter(pk=self.quote.pk).update(status=QuoteStatus.DRAFT)
        self.config.quote = None
        self.config.save()
        self.assertIn(
            self.project_file.reference or self.project_file.name, self._description(),
        )


class VatGroupTests(_AdvanceTestBase):
    def test_one_line_per_vat_group(self):
        self.add_config(self.quote, Decimal("500.00"), percentage=Decimal("10"))

        invoice = _create(self.project_file, percentage=Decimal("30"))

        lines = list(invoice.items.order_by("pk"))
        self.assertEqual([line.unit_price.amount for line in lines], [Decimal("300.00"), Decimal("150.00")])
        self.assertEqual(
            [_line_taxes(line) for line in lines],
            [
                [("IVA", Decimal("21.00"), Decimal("300.00"), Decimal("63.00"))],
                [("IVA", Decimal("10.00"), Decimal("150.00"), Decimal("15.00"))],
            ],
        )
        # Several groups: each line names its taxes
        self.assertNotEqual(lines[0].description, lines[1].description)
        self.assertEqual(
            _invoice_taxes(invoice),
            [
                ("IVA", Decimal("21.00"), Decimal("300.00"), Decimal("63.00"), 0),
                ("IVA", Decimal("10.00"), Decimal("150.00"), Decimal("15.00"), 1),
            ],
        )
        self.assertEqual(
            _totals(invoice),
            (Decimal("450.00"), Decimal("0.00"), Decimal("450.00"), Decimal("78.00"), Decimal("528.00")),
        )

    def test_retention_and_equivalence_surcharge_groups(self):
        irpf = self._tax_type("IRPF", behavior="subtract")
        recargo = self._tax_type("RE")
        self._add_tax(self.config, irpf, Decimal("15"))
        other = self.add_config(self.quote, Decimal("500.00"))
        self._add_tax(other, recargo, Decimal("5.2"))

        invoice = _create(self.project_file, percentage=Decimal("30"))

        lines = list(invoice.items.order_by("pk"))
        self.assertEqual(
            [_line_taxes(line) for line in lines],
            [
                [
                    ("IVA", Decimal("21.00"), Decimal("300.00"), Decimal("63.00")),
                    ("IRPF", Decimal("15.00"), Decimal("300.00"), Decimal("45.00")),
                ],
                [
                    ("IVA", Decimal("21.00"), Decimal("150.00"), Decimal("31.50")),
                    ("RE", Decimal("5.20"), Decimal("150.00"), Decimal("7.80")),
                ],
            ],
        )
        # IVA on both groups; IRPF and RE only on theirs
        self.assertEqual(
            _invoice_taxes(invoice),
            [
                ("IVA", Decimal("21.00"), Decimal("450.00"), Decimal("94.50"), 0),
                ("IRPF", Decimal("15.00"), Decimal("300.00"), Decimal("45.00"), 1),
                ("RE", Decimal("5.20"), Decimal("150.00"), Decimal("7.80"), 2),
            ],
        )
        # Base + IVA + RE − IRPF
        self.assertEqual(
            _totals(invoice),
            (Decimal("450.00"), Decimal("0.00"), Decimal("450.00"), Decimal("57.30"), Decimal("507.30")),
        )
        preview = AdvanceInvoiceService.preview(self.project_file, percentage=Decimal("30"))
        self.assertEqual((preview["tax_amount"], preview["total"]), (Decimal("57.30"), Decimal("507.30")))

    def test_untaxed_lines_form_their_own_group(self):
        config = self.add_config(self.quote, Decimal("200.00"))
        AppliedTax.objects.filter(
            content_type=ContentType.objects.get_for_model(Configuration), object_id=config.pk,
        ).delete()

        invoice = _create(self.project_file, percentage=Decimal("50"))

        lines = list(invoice.items.order_by("pk"))
        self.assertEqual([line.unit_price.amount for line in lines], [Decimal("500.00"), Decimal("100.00")])
        self.assertEqual(_line_taxes(lines[1]), [])
        self.assertEqual(
            _invoice_taxes(invoice), [("IVA", Decimal("21.00"), Decimal("500.00"), Decimal("105.00"), 0)],
        )
        self.assertEqual(_totals(invoice)[4], Decimal("705.00"))


@override_settings(AEAT_SUBMISSIONS_ENABLED=False)
class GroupInvoicedFirstTests(_AdvanceTestBase):
    """The 1,000.00 configuration at IVA 21 % plus 200.00 of another VAT
    group whose goods are invoiced before the advance: the advance has
    nothing of that group left to prepay, so all of it goes on the goods
    still to invoice, and the closing invoice deducts all of it."""

    def _invoice(self, config):
        """A pro forma of ``config`` in full, its advances deducted, issued."""
        invoice = ProjectFileInvoiceService.create_invoice_from_project_file(
            project_file=self.project_file, company=self.company, client=self.client_obj,
            date_issued=date(2026, 9, 29), due_date=None,
            items_data=[{
                "source_id": f"config_{config.pk}", "description": config.name,
                "quantity": Decimal("1"), "unit_price": config.subtotal_price.amount,
                "discount_percentage": Decimal("0.00"),
            }],
        )
        AdvanceDeductionService.apply(invoice)
        return SalesInvoiceService.finalize_invoice(invoice, send_email=False)

    def _advance_then_rest(self, other):
        first = self._invoice(other)
        advance = SalesInvoiceService.finalize_invoice(
            _create(self.project_file, percentage=Decimal("30")), send_email=False,
        )
        rest = self._invoice(self.config)
        return first, advance, rest

    def test_another_rate(self):
        other = self.add_config(self.quote, Decimal("200.00"), percentage=Decimal("10"))

        first, advance, rest = self._advance_then_rest(other)

        # 30 % of 1,200.00, all of it on the goods at IVA 21 %
        [line] = advance.items.all()
        self.assertEqual(
            _line_taxes(line), [("IVA", Decimal("21.00"), Decimal("360.00"), Decimal("75.60"))],
        )
        self.assertEqual(advance.total_amount.amount, Decimal("435.60"))
        self.assertEqual(rest.total_amount.amount, Decimal("774.40"))
        # The goods, billed once: 1,000.00 × 1.21 + 200.00 × 1.10
        self.assertEqual(
            sum(invoice.total_amount.amount for invoice in (first, advance, rest)), Decimal("1430.00"),
        )
        summary = ProjectFileInvoiceService.get_invoiceable_summary(self.project_file)
        self.assertEqual((summary["goods_remaining_net"], summary["advances_open_net"]), (Decimal("0.00"),) * 2)

    def test_untaxed_goods(self):
        other = self.add_config(self.quote, Decimal("200.00"))
        AppliedTax.objects.filter(
            content_type=ContentType.objects.get_for_model(Configuration), object_id=other.pk,
        ).delete()

        _first, advance, rest = self._advance_then_rest(other)

        [line] = advance.items.all()
        self.assertEqual(
            _line_taxes(line), [("IVA", Decimal("21.00"), Decimal("360.00"), Decimal("75.60"))],
        )
        # No untaxed deduction: the IVA base is the whole taxable base
        self.assertEqual(
            _invoice_taxes(rest), [("IVA", Decimal("21.00"), Decimal("640.00"), Decimal("134.40"), 0)],
        )
        self.assertEqual(rest.taxable_base.amount, Decimal("640.00"))
        self.assertEqual(
            ProjectFileInvoiceService.get_invoiceable_summary(self.project_file)["advances_open_net"],
            Decimal("0.00"),
        )

    def test_the_largest_advance_allowed(self):
        other = self.add_config(self.quote, Decimal("200.00"), percentage=Decimal("10"))
        self._invoice(other)

        preview = AdvanceInvoiceService.preview(self.project_file, percentage=Decimal("100"))

        # The 200.00 is invoiced: 100 % of 1,200.00 no longer fits
        self.assertEqual(preview["max_amount"], Decimal("1000.00"))
        self.assertTrue(preview["error"])
        preview = AdvanceInvoiceService.preview(self.project_file, amount=Decimal("1000.00"))
        self.assertIsNone(preview["error"])
        self.assertEqual([line["base"] for line in preview["lines"]], [Decimal("1000.00")])


class AmountModeTests(_AdvanceTestBase):
    def test_amount_is_split_by_largest_remainder(self):
        self.add_config(self.quote, Decimal("500.00"), percentage=Decimal("10"))

        invoice = _create(self.project_file, amount=Decimal("500.00"))

        # 333.333… + 166.666…: the leftover cent goes to the larger remainder
        self.assertEqual(
            [line.unit_price.amount for line in invoice.items.order_by("pk")],
            [Decimal("333.33"), Decimal("166.67")],
        )
        self.assertEqual(invoice.subtotal_amount.amount, Decimal("500.00"))
        self.assertEqual(invoice.advance_percentage, Decimal("33.3333"))

    def test_amount_wins_over_percentage(self):
        invoice = _create(self.project_file, percentage=Decimal("90"), amount=Decimal("250.00"))

        self.assertEqual(invoice.subtotal_amount.amount, Decimal("250.00"))
        self.assertEqual(invoice.advance_percentage, Decimal("25.0000"))

    def test_split_adds_up(self):
        groups = {frozenset({"a"}): Decimal("1.00"), frozenset({"b"}): Decimal("1.00"),
                  frozenset({"c"}): Decimal("1.00")}
        shares = AdvanceInvoiceService._split(Decimal("0.05"), groups)
        self.assertEqual(sum(shares.values()), Decimal("0.05"))
        self.assertEqual(sorted(shares.values()), [Decimal("0.01"), Decimal("0.02"), Decimal("0.02")])


class RefusedAdvanceTests(_AdvanceTestBase):
    def _assert_refused(self, **kwargs):
        count = SalesInvoice.objects.count()
        with self.assertRaises(ValidationError):
            _create(self.project_file, **kwargs)
        self.assertEqual(SalesInvoice.objects.count(), count)
        preview = AdvanceInvoiceService.preview(self.project_file, **kwargs)
        self.assertTrue(preview["error"])
        self.assertEqual(preview["lines"], [])

    def test_invalid_percentages(self):
        for percentage in ("0", "-5", "100.01", "150"):
            with self.subTest(percentage=percentage):
                self._assert_refused(percentage=Decimal(percentage))

    def test_invalid_amounts(self):
        for amount in ("0", "-10"):
            with self.subTest(amount=amount):
                self._assert_refused(amount=Decimal(amount))

    def test_nothing_entered(self):
        self._assert_refused()

    def test_a_hundred_percent_is_allowed(self):
        invoice = _create(self.project_file, percentage=Decimal("100"))
        self.assertEqual(invoice.subtotal_amount.amount, Decimal("1000.00"))

    def test_cap_counts_proforma_advances(self):
        _create(self.project_file, percentage=Decimal("60"))

        self._assert_refused(percentage=Decimal("60"))
        preview = AdvanceInvoiceService.preview(self.project_file, percentage=Decimal("60"))
        self.assertEqual(preview["max_amount"], Decimal("400.00"))

        # Exactly what is left fits
        self.assertEqual(
            _create(self.project_file, amount=Decimal("400.00")).subtotal_amount.amount,
            Decimal("400.00"),
        )
        self._assert_refused(amount=Decimal("0.01"))

    def test_cap_counts_invoiced_goods(self):
        self.invoice_goods(ratio=Decimal("0.5"))

        self._assert_refused(percentage=Decimal("60"))
        self.assertEqual(
            AdvanceInvoiceService.preview(self.project_file, percentage=Decimal("50"))["error"], None,
        )

    def test_cancelled_advances_free_the_cap(self):
        advance, _line = self.make_advance(Decimal("600.00"), "60")
        self._assert_refused(percentage=Decimal("60"))
        self.set_status(advance, SalesInvoiceStatus.CANCELLED)

        self.assertEqual(
            _create(self.project_file, percentage=Decimal("60")).subtotal_amount.amount,
            Decimal("600.00"),
        )

    def test_empty_expediente(self):
        project_file = ProjectFile.objects.create(
            name="Vacío", company=self.company, client=self.client_obj,
        )
        with self.assertRaises(ValidationError):
            _create(project_file, percentage=Decimal("30"))


class CompanyTests(_AdvanceTestBase):
    def test_invoice_is_the_expedientes_company(self):
        invoice = _create(self.project_file, percentage=Decimal("30"))
        self.assertEqual(invoice.company, self.project_file.company)

    def test_wizard_refuses_another_company(self):
        other = Company.objects.create(name="Zeta Obras")
        summary = ProjectFileInvoiceService.get_invoiceable_summary(self.project_file)
        data = {
            "company": other.pk,
            "date_issued": "2026-09-29",
            "invoice_mode": "advance",
            "percentage": "30",
            "payment_method": PaymentMethod.TRANSFER,
        }

        form = ProjectFileInvoiceSetupForm(
            data=data, project_file=self.project_file, invoicing_summary=summary,
        )
        self.assertFalse(form.is_valid())
        self.assertEqual(list(form.errors), ["company"])

        data["company"] = self.company.pk
        form = ProjectFileInvoiceSetupForm(
            data=data, project_file=self.project_file, invoicing_summary=summary,
        )
        self.assertTrue(form.is_valid(), form.errors)

    def test_wizard_maps_the_error_to_the_field_entered(self):
        summary = ProjectFileInvoiceService.get_invoiceable_summary(self.project_file)
        base = {
            "company": self.company.pk,
            "date_issued": "2026-09-29",
            "invoice_mode": "advance",
            "payment_method": PaymentMethod.TRANSFER,
        }
        for extra, field in (
            ({"percentage": "120"}, "percentage"),
            ({"percentage": "30", "custom_amount": "5000"}, "custom_amount"),
        ):
            with self.subTest(**extra):
                form = ProjectFileInvoiceSetupForm(
                    data={**base, **extra}, project_file=self.project_file,
                    invoicing_summary=summary,
                )
                self.assertFalse(form.is_valid())
                self.assertEqual(list(form.errors), [field])


@override_settings(AEAT_SUBMISSIONS_ENABLED=False)
class FinalizeAdvanceTests(_AdvanceTestBase):
    def test_finalize_issues_the_advance(self):
        invoice = _create(self.project_file, percentage=Decimal("30"))

        invoice = SalesInvoiceService.finalize_invoice(invoice, send_email=False)

        invoice.refresh_from_db()
        self.assertEqual(invoice.status, SalesInvoiceStatus.FINAL)
        self.assertTrue(invoice.invoice_number)
        self.assertEqual(invoice.total_amount.amount, Decimal("363.00"))

    def test_finalize_rechecks_the_cap(self):
        invoice = _create(self.project_file, percentage=Decimal("60"))
        # The expediente shrinks below the advance
        Configuration.objects.filter(pk=self.config.pk).update(
            subtotal_price=Decimal("500.00"), total_price=Decimal("605.00"),
        )
        self.quote.recalc_totals()

        with self.assertRaises(ValidationError):
            SalesInvoiceService.finalize_invoice(invoice, send_email=False)

        invoice.refresh_from_db()
        self.assertEqual(invoice.status, SalesInvoiceStatus.PROFORMA)
        self.assertIsNone(invoice.invoice_number)

    def test_finalize_rechecks_each_vat_group(self):
        other = self.add_config(self.quote, Decimal("200.00"), percentage=Decimal("10"))
        invoice = _create(self.project_file, percentage=Decimal("30"))
        # The 10 % goods leave the expediente: their 60.00 would have
        # nothing to be deducted from, though the 360.00 still fit in total
        Configuration.objects.filter(pk=other.pk).update(
            subtotal_price=Decimal("0.00"), total_price=Decimal("0.00"),
        )
        self.quote.recalc_totals()

        with self.assertRaises(ValidationError):
            SalesInvoiceService.finalize_invoice(invoice, send_email=False)

        invoice.refresh_from_db()
        self.assertEqual(invoice.status, SalesInvoiceStatus.PROFORMA)

    def test_other_open_advances_count(self):
        first = _create(self.project_file, percentage=Decimal("50"))
        second = _create(self.project_file, percentage=Decimal("50"))
        Configuration.objects.filter(pk=self.config.pk).update(
            subtotal_price=Decimal("800.00"), total_price=Decimal("968.00"),
        )
        self.quote.recalc_totals()

        # 500 + 500 no longer fit 800, whichever is issued first
        for invoice in (first, second):
            with self.subTest(invoice=invoice.pk), self.assertRaises(ValidationError):
                SalesInvoiceService.finalize_invoice(invoice, send_email=False)

        second.delete()
        SalesInvoiceService.finalize_invoice(first, send_email=False)
        first.refresh_from_db()
        self.assertEqual(first.status, SalesInvoiceStatus.FINAL)


class AdvanceLockOrderTests(_AdvanceTestBase):
    def test_expediente_then_advances_then_numbering(self):
        _create(self.project_file, percentage=Decimal("30"))

        with CaptureQueriesContext(connection) as ctx:
            _create(self.project_file, percentage=Decimal("30"))

        locked = [
            (re.search(r'FROM "(\w+)"', q["sql"]).group(1), q["sql"].split(" WHERE ", 1)[1])
            for q in ctx.captured_queries
            if "FOR UPDATE" in q["sql"]
        ]
        self.assertEqual(locked[0][0], "management_projectfile")
        # The advances, then the numbering row
        self.assertEqual(locked[1][0], "invoices_salesinvoice")
        self.assertIn('"document_type" =', locked[1][1])
        self.assertEqual(locked[2][0], "invoices_salesinvoice")
        self.assertIn('"proforma_incremental_id" IS NOT NULL', locked[2][1])


@override_settings(AEAT_SUBMISSIONS_ENABLED=False)
class AdvanceWizardTests(_AdvanceTestBase):
    STEP = "project_file_invoice_wizard_view-current_step"

    def setUp(self):
        super().setUp()
        self.client.force_login(User.objects.create_superuser("wizard", "w@x.com", "pw"))
        self.url = reverse("admin:management_projectfile_create_invoice", args=[self.project_file.pk])

    def _setup(self, **fields):
        self.client.get(self.url)
        return self.client.post(self.url, {
            self.STEP: "setup",
            "setup-company": self.company.pk,
            "setup-date_issued": "2026-09-29",
            "setup-due_date": "",
            "setup-invoice_mode": "advance",
            "setup-percentage": "30",
            "setup-custom_amount": "",
            "setup-payment_method": PaymentMethod.TRANSFER,
            **{f"setup-{name}": value for name, value in fields.items()},
        })

    def test_advance_skips_the_items_step(self):
        response = self._setup()

        self.assertEqual(response.status_code, 200)
        wizard = response.context["wizard"]
        self.assertEqual(wizard["steps"].current, "notes")
        self.assertEqual(wizard["steps"].count, 2)
        self.assertIn("finalize", wizard["form"].fields)
        preview = response.context["advance_preview"]
        self.assertIsNone(preview["error"])
        self.assertEqual(
            (preview["base"], preview["tax_amount"], preview["total"]),
            (Decimal("300.00"), Decimal("63.00"), Decimal("363.00")),
        )

        response = self.client.post(self.url, {self.STEP: "notes", "notes-notes": "Anticipo"})

        invoice = SalesInvoice.objects.get(document_type=SalesInvoiceDocumentType.ADVANCE)
        self.assertRedirects(
            response, reverse("admin:invoices_salesinvoice_change", args=[invoice.pk]),
            fetch_redirect_response=False,
        )
        self.assertEqual(invoice.status, SalesInvoiceStatus.PROFORMA)
        self.assertEqual(invoice.notes, "Anticipo")
        self.assertEqual(invoice.total_amount.amount, Decimal("363.00"))
        self.assertEqual(invoice.items.get().unit_price.amount, Decimal("300.00"))

    def test_amount_mode(self):
        self._setup(percentage="", custom_amount="250")
        self.client.post(self.url, {self.STEP: "notes", "notes-notes": ""})

        invoice = SalesInvoice.objects.get(document_type=SalesInvoiceDocumentType.ADVANCE)
        self.assertEqual(invoice.subtotal_amount.amount, Decimal("250.00"))
        self.assertEqual(invoice.advance_percentage, Decimal("25.0000"))

    def test_issue_as_final_now(self):
        self._setup()
        self.client.post(self.url, {self.STEP: "notes", "notes-notes": "", "notes-finalize": "on"})

        invoice = SalesInvoice.objects.get(document_type=SalesInvoiceDocumentType.ADVANCE)
        self.assertEqual(invoice.status, SalesInvoiceStatus.FINAL)
        self.assertTrue(invoice.invoice_number)

    def test_refused_advance_stays_on_the_setup_step(self):
        response = self._setup(percentage="120")

        self.assertEqual(response.context["wizard"]["steps"].current, "setup")
        self.assertIn("percentage", response.context["wizard"]["form"].errors)
        self.assertFalse(SalesInvoice.objects.exists())

    def test_goods_invoices_keep_the_items_step(self):
        response = self._setup(invoice_mode="percentage", percentage="100")

        self.assertEqual(response.context["wizard"]["steps"].current, "items")
        self.assertEqual(response.context["wizard"]["steps"].count, 3)

    def test_setup_step_links_the_live_preview(self):
        response = self.client.get(self.url)
        self.assertEqual(
            response.context["advance_preview_url"],
            reverse("admin:management_projectfile_advance_preview", args=[self.project_file.pk]),
        )

    def test_setup_step_carries_the_advance_labels(self):
        # The page swaps them in for the goods ones in advance mode
        form = self.client.get(self.url).context["wizard"]["form"]

        for name in ("percentage", "custom_amount"):
            with self.subTest(field=name):
                attrs = form.fields[name].widget.attrs
                self.assertTrue(attrs["data-advance-label"])
                self.assertTrue(attrs["data-advance-help"])
                self.assertNotEqual(str(attrs["data-advance-label"]), str(form.fields[name].label))
                self.assertIn("data-advance-label=", str(form[name]))

    def test_other_companies_expedientes_are_not_found(self):
        user = User.objects.create_user("staff", password="pw", is_staff=True)
        CompanyUser.objects.create(user=user, company=self.company, is_active=True)
        user.user_permissions.add(Permission.objects.get(codename="add_salesinvoice"))
        self.client.force_login(user)
        other = Company.objects.create(name="Zeta Obras")
        other_client = Client.objects.create(name="Otro", company=other)
        project_file = ProjectFile.objects.create(name="Ajeno", company=other, client=other_client)
        url = reverse("admin:management_projectfile_create_invoice", args=[project_file.pk])

        self.assertEqual(self.client.get(url).status_code, 404)
        response = self.client.post(url, {
            self.STEP: "setup",
            "setup-company": self.company.pk,
            "setup-date_issued": "2026-09-29",
            "setup-invoice_mode": "percentage",
            "setup-percentage": "100",
            "setup-payment_method": PaymentMethod.TRANSFER,
        })
        self.assertEqual(response.status_code, 404)
        self.assertFalse(SalesInvoice.objects.exists())
        # Its own company's expediente still opens
        self.assertEqual(self.client.get(self.url).status_code, 200)


class AdvancePreviewUrlTests(_AdvanceTestBase):
    def setUp(self):
        super().setUp()
        self.url = reverse("admin:management_projectfile_advance_preview", args=[self.project_file.pk])

    def test_json_preview(self):
        self.client.force_login(User.objects.create_superuser("wizard", "w@x.com", "pw"))

        data = self.client.get(self.url, {"percentage": "30"}).json()

        self.assertIsNone(data["error"])
        self.assertEqual(
            (Decimal(data["base"]), Decimal(data["tax_amount"]), Decimal(data["total"])),
            (Decimal("300.00"), Decimal("63.00"), Decimal("363.00")),
        )
        self.assertEqual(Decimal(data["max_amount"]), Decimal("1000.00"))
        self.assertEqual(len(data["lines"]), 1)
        self.assertFalse(SalesInvoice.objects.exists())

        # Typed with a decimal comma; an amount wins over a percentage
        data = self.client.get(self.url, {"percentage": "30", "amount": "250,50"}).json()
        self.assertEqual(Decimal(data["base"]), Decimal("250.50"))

        data = self.client.get(self.url, {"percentage": "abc"}).json()
        self.assertTrue(data["error"])

    def test_needs_the_add_invoice_permission(self):
        user = User.objects.create_user("staff", password="pw", is_staff=True)
        CompanyUser.objects.create(user=user, company=self.company, is_active=True)
        self.client.force_login(user)
        self.assertEqual(self.client.get(self.url, {"percentage": "30"}).status_code, 403)

        user.user_permissions.add(Permission.objects.get(codename="add_salesinvoice"))
        self.client.force_login(User.objects.get(pk=user.pk))
        response = self.client.get(self.url, {"percentage": "30"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Decimal(response.json()["total"]), Decimal("363.00"))

    def test_other_companies_expedientes_are_not_found(self):
        self.client.force_login(User.objects.create_superuser("wizard", "w@x.com", "pw"))
        other = Company.objects.create(name="Zeta Obras")
        other_client = Client.objects.create(name="Otro", company=other)
        project_file = ProjectFile.objects.create(name="Ajeno", company=other, client=other_client)

        response = self.client.get(
            reverse("admin:management_projectfile_advance_preview", args=[project_file.pk]),
            {"percentage": "30"},
        )
        self.assertEqual(response.status_code, 404)
