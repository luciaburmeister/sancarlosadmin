"""
Deducting advances on the invoices that bill an expediente's goods
(AdvanceDeductionService) and the flows that use it:

- the user's expediente: "100 % del pendiente" after a 30 % advance bills
  the goods in full less 457.90, with the quote's discount unchanged, and
  advance + rest add up to one invoice for everything;
- a deduction line stays out of the discount basket;
- the default plan: proportional per VAT group, then the closing sweep
  (every cent, whatever the groups' figures drifted by); the minimum; caps
  when the expediente shrank; which advances are offered; an untaxed
  advance line only lowers untaxed goods;
- goods billed by another company or to another client than an advance
  pending are refused;
- the lines' text is stored in the documents' language;
- amounts are checked against the server's plan, never trusted as posted;
- remove / reapply restore the tax bases (service and admin actions, which
  only run on their dialog's POST);
- the admin inline refuses to edit or delete advance rows, and the tax
  bases are read-only on invoices that have them; an advance takes no new
  line and its taxes cannot be changed there;
- the invoice wizard's "advances to deduct" step (flow A);
- invoicing a configuration (flow F) is refused while advances are pending.
"""
from datetime import date
from decimal import Decimal
from unittest import mock

from django.conf import settings
from django.contrib import admin, messages
from django.contrib.auth.models import Permission, User
from django.contrib.contenttypes.models import ContentType
from django.contrib.messages import get_messages
from django.core.exceptions import ValidationError
from django.forms import MultiWidget
from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse
from django.utils import translation
from djmoney.money import Money

from company.models import Company
from configurator.models import Configuration
from invoices.admin import AppliedTaxInline, SalesInvoiceItemInline
from invoices.forms.project_file_wizard_forms import ProjectFileInvoiceSetupForm
from invoices.models import (
    AppliedTax,
    SalesInvoice,
    SalesInvoiceItem,
    SalesInvoiceStatus,
    TaxType,
)
from invoices.services.advances import AdvanceDeductionService, AdvanceInvoiceService
from invoices.services.advances.deductions import InvoiceGoods, _Room
from invoices.services.discounts import ADVANCE_LINE_PREFIX, QuoteDiscountResolver, SourceLine
from invoices.services.project_file_invoice_service import ProjectFileInvoiceService
from invoices.services.sales_invoice_service import SalesInvoiceService
from invoices.tests.advance_fixtures import (
    WIZARD_STEP,
    AdvanceExpedienteMixin,
    UserExpedienteMixin,
    full_items,
    invoice_taxes,
    totals,
)
from materials.models import PaymentMethod
from production.models import ProductionTicket
from sales.models import (
    Client,
    DiscountKind,
    DiscountLevel,
    DiscountMode,
    DiscountPolicy,
    DiscountRule,
)


class UserExpedienteRestInvoiceTests(UserExpedienteMixin, TestCase):
    """The worked example: a 30 % advance (457.90 + IVA = 554.06), then
    "100 % del pendiente"."""

    def setUp(self):
        super().setUp()
        self.advance = self.issue_advance()

    def test_rest_invoice_bills_the_goods_in_full_less_the_advance(self):
        items = full_items(self.project_file)
        rest = self.rest_invoice()

        goods = rest.items.filter(advance_line__isnull=True)
        self.assertEqual(
            sorted(line.line_total.amount for line in goods),
            sorted(ProjectFileInvoiceService.line_total(item) for item in items),
        )
        [deduction] = AdvanceDeductionService.deduction_lines(rest)
        [advance_line] = self.advance.items.all()
        self.assertEqual(deduction.advance_line, advance_line)
        self.assertEqual(deduction.source_id, f"advance_ded_{advance_line.pk}")
        self.assertEqual((deduction.quantity, deduction.unit_price.amount), (Decimal("1.00"), Decimal("-457.90")))
        self.assertIsNone(deduction.project_file)  # the invoice FK attributes it
        self.assertIn("INV-2026-00012", deduction.description)
        self.assertIn("03/09/2026", deduction.description)

        # The quote's discount lands complete and unchanged
        self.assertEqual(rest.discount_breakdown, [
            {**entry, "quote_id": self.quote.pk} for entry in self.quote.discount_breakdown
        ])
        [entry] = rest.discount_breakdown
        self.assertEqual(
            (Decimal(str(entry["base"])), Decimal(str(entry["amount"]))),
            (Decimal("1557.49"), Decimal("31.15")),
        )
        self.assertEqual(
            totals(rest),
            (Decimal("1099.59"), Decimal("31.15"), Decimal("1068.44"), Decimal("224.37"), Decimal("1292.81")),
        )
        self.assertEqual(invoice_taxes(rest), {("IVA", Decimal("21.00")): (Decimal("1068.44"), Decimal("224.37"))})

    def test_advance_and_rest_add_up_to_one_invoice(self):
        rest = self.rest_invoice()
        self.advance.refresh_from_db()

        self.assertEqual(self.advance.total_amount.amount + rest.total_amount.amount, Decimal("1846.87"))
        self.assertEqual(self.advance.tax_amount.amount + rest.tax_amount.amount, Decimal("320.53"))
        self.assertEqual(
            self.advance.taxable_base.amount + rest.taxable_base.amount, Decimal("1526.34"),
        )
        summary = ProjectFileInvoiceService.get_invoiceable_summary(self.project_file)
        self.assertEqual(summary["total_invoiceable"], Decimal("1846.87"))
        self.assertEqual(summary["remaining"], Decimal("0.00"))
        self.assertEqual(summary["goods_remaining_net"], Decimal("0.00"))
        self.assertEqual(summary["advances_open_net"], Decimal("0.00"))
        self.project_file.refresh_from_db()
        self.assertEqual(self.project_file.percentage_invoiced, Decimal("100.00"))
        self.assertEqual(self.project_file.advance_pending_amount.amount, Decimal("0.00"))

    def test_resolver_ignores_a_deduction_line(self):
        items = full_items(self.project_file)
        summary = ProjectFileInvoiceService.get_invoiceable_summary(self.project_file)
        quotes = [data["quote"] for data in summary["quotes"]]
        priced = ProjectFileInvoiceService.price_lines(summary, items).resolution
        [advance_line] = self.advance.items.all()
        source_lines = [
            SourceLine(item["source_id"], ProjectFileInvoiceService.line_total(item), self.quote.pk)
            for item in items
        ]

        without = QuoteDiscountResolver().resolve(quotes, source_lines)
        with_deduction = QuoteDiscountResolver().resolve(
            quotes, source_lines + [SourceLine(f"advance_ded_{advance_line.pk}", Decimal("-457.90"), None)],
        )

        self.assertEqual(with_deduction.discount_amount, without.discount_amount)
        self.assertEqual(with_deduction.breakdown, without.breakdown)
        self.assertEqual(with_deduction._fractions, without._fractions)
        self.assertEqual(without.breakdown, priced.breakdown)


class _DeductionTestBase(AdvanceExpedienteMixin, TestCase):
    """An expediente whose quote holds a 1,000.00 configuration at IVA 21 %."""

    def goods(self, ratio=Decimal("1")):
        """A pro forma billing ``ratio`` of the expediente's goods, no deduction yet."""
        return self.invoice_goods(ratio)

    def plan(self, ratio=Decimal("1")):
        items = ProjectFileInvoiceService.build_items_for_ratio(self.summary(), ratio)
        return AdvanceDeductionService.preview_plan(
            self.project_file, items, company=self.company, client=self.client_obj,
        )

    def bases(self, invoice):
        return {key: base for key, (base, _amount) in invoice_taxes(invoice).items()}


class ClosingInvoiceTests(_DeductionTestBase):
    def test_takes_the_whole_advance(self):
        _, line = self.make_advance(Decimal("300.00"), "30")
        invoice = self.goods()

        [deduction] = AdvanceDeductionService.apply(invoice)

        self.assertEqual(deduction.advance_line, line)
        self.assertEqual(deduction.line_total.amount, Decimal("-300.00"))
        self.assertEqual(
            totals(invoice),
            (Decimal("700.00"), Decimal("0.00"), Decimal("700.00"), Decimal("147.00"), Decimal("847.00")),
        )
        self.assertEqual(self.bases(invoice), {("IVA", Decimal("21.00")): Decimal("700.00")})
        summary = self.summary()
        self.assertEqual(summary["goods_remaining_net"], Decimal("0.00"))
        self.assertEqual(summary["advances_open_net"], Decimal("0.00"))

    def test_its_row_is_fixed(self):
        _, line = self.make_advance(Decimal("300.00"), "30")

        [row] = self.plan()

        self.assertEqual(row.line, line)
        self.assertEqual((row.minimum, row.amount, row.maximum), (Decimal("300.00"),) * 3)
        self.assertTrue(row.fixed)

    def test_the_deduction_cannot_be_skipped_or_lowered(self):
        _, line = self.make_advance(Decimal("300.00"), "30")
        invoice = self.goods()

        for amounts in ({}, {line.pk: Decimal("200.00")}, {line.pk: Decimal("300.01")}):
            with self.subTest(amounts=amounts), self.assertRaises(ValidationError):
                AdvanceDeductionService.apply(invoice, amounts)
        self.assertEqual(AdvanceDeductionService.deduction_lines(invoice), [])

        AdvanceDeductionService.apply(invoice, {line.pk: Decimal("300.00")})
        self.assertEqual(totals(invoice)[-1], Decimal("847.00"))

    def test_a_hundred_percent_advance_leaves_a_zero_invoice(self):
        self.make_advance(Decimal("1000.00"), "100")
        invoice = self.goods()

        AdvanceDeductionService.apply(invoice)

        self.assertEqual(totals(invoice), (Decimal("0.00"),) * 5)
        self.assertEqual(self.bases(invoice), {("IVA", Decimal("21.00")): Decimal("0.00")})


class PartialInvoiceTests(_DeductionTestBase):
    def test_deducts_its_share_and_the_closing_invoice_the_rest(self):
        self.add_config(self.quote, Decimal("500.00"), percentage=Decimal("10"))
        advance = AdvanceInvoiceService.create(
            self.project_file, date_issued=date(2026, 9, 1), percentage=Decimal("30"),
        )
        self.set_status(advance, SalesInvoiceStatus.FINAL)
        line_21 = advance.items.get(unit_price=Decimal("300.00"))
        line_10 = advance.items.get(unit_price=Decimal("150.00"))

        first = self.goods(Decimal("0.5"))
        AdvanceDeductionService.apply(first)
        self.assertEqual(
            {d.advance_line: d.line_total.amount for d in AdvanceDeductionService.deduction_lines(first)},
            {line_21: Decimal("-150.00"), line_10: Decimal("-75.00")},
        )
        self.assertEqual(self.bases(first), {
            ("IVA", Decimal("21.00")): Decimal("350.00"),
            ("IVA", Decimal("10.00")): Decimal("175.00"),
        })

        rest = self.goods(Decimal("0.5"))
        [row_21, row_10] = sorted(
            AdvanceDeductionService.default_plan(rest),
            key=lambda row: -row.pending,
        )
        self.assertTrue(row_21.fixed and row_10.fixed)
        AdvanceDeductionService.apply(rest)
        self.assertEqual(
            {d.advance_line: d.line_total.amount for d in AdvanceDeductionService.deduction_lines(rest)},
            {line_21: Decimal("-150.00"), line_10: Decimal("-75.00")},
        )
        summary = self.summary()
        self.assertEqual(summary["goods_remaining_net"], Decimal("0.00"))
        self.assertEqual(summary["advances_open_net"], Decimal("0.00"))

    def test_the_minimum_keeps_open_advances_within_the_goods_left(self):
        _, line = self.make_advance(Decimal("800.00"), "80")

        # 500 of goods leaves 500 to invoice: at least 300 of the 800 must go
        [row] = self.plan(Decimal("0.5"))
        self.assertEqual(
            (row.minimum, row.amount, row.maximum),
            (Decimal("300.00"), Decimal("400.00"), Decimal("500.00")),
        )
        self.assertFalse(row.fixed)

        invoice = self.goods(Decimal("0.5"))
        with self.assertRaises(ValidationError):
            AdvanceDeductionService.apply(invoice, {line.pk: Decimal("299.99")})
        [deduction] = AdvanceDeductionService.apply(invoice, {line.pk: Decimal("300.00")})
        self.assertEqual(deduction.line_total.amount, Decimal("-300.00"))

    def test_can_be_postponed_to_the_closing_invoice(self):
        _, line = self.make_advance(Decimal("300.00"), "30")
        first = self.goods(Decimal("0.5"))

        self.assertEqual(AdvanceDeductionService.apply(first, {}), [])
        self.assertEqual(totals(first)[-1], Decimal("605.00"))

        rest = self.goods(Decimal("0.5"))
        [deduction] = AdvanceDeductionService.apply(rest)
        self.assertEqual((deduction.advance_line, deduction.line_total.amount), (line, Decimal("-300.00")))

    def test_a_lowered_amount_leaves_the_rest_pending(self):
        _, line = self.make_advance(Decimal("300.00"), "30")
        invoice = self.goods(Decimal("0.5"))

        AdvanceDeductionService.apply(invoice, {str(line.pk): "100.00"})

        self.assertEqual(self.summary()["advances_open_net"], Decimal("200.00"))


class SummaryWithoutAnInvoiceTests(_DeductionTestBase):
    def test_the_expediente_as_if_the_invoice_did_not_exist(self):
        self.make_advance(Decimal("300.00"), "30")
        other = self.goods(Decimal("0.5"))
        AdvanceDeductionService.apply(other, {})
        rest = self.goods(Decimal("0.5"))
        AdvanceDeductionService.apply(rest)

        summary = self.summary()
        self.assertEqual((summary["goods_remaining_net"], summary["advances_open_net"]), (Decimal("0.00"),) * 2)

        summary = self.summary(exclude_invoice=rest)
        self.assertEqual(summary["goods_remaining_net"], Decimal("500.00"))
        self.assertEqual(summary["advances_open_net"], Decimal("300.00"))
        self.assertEqual(summary["advances"][0]["lines"][0]["deducted"], Decimal("0.00"))
        self.assertNotIn(rest.pk, [entry["id"] for entry in summary["invoices"]])


class OfferedAdvancesTests(_DeductionTestBase):
    def test_a_pro_forma_advance_is_not_offered(self):
        # Goods cannot be invoiced once a pro forma advance exists: invoiced before it
        invoice = self.goods(Decimal("0.5"))
        self.make_advance(Decimal("300.00"), "30", status=SalesInvoiceStatus.PROFORMA)

        self.assertEqual(self.plan(), [])
        self.assertEqual(AdvanceDeductionService.apply(invoice), [])

    def test_an_advance_of_another_client_or_company_is_not_offered(self):
        other_client = Client.objects.create(name="Otro", company=self.company)
        for field, value in (("client", other_client), ("company", self.make_company())):
            with self.subTest(field=field):
                advance, _ = self.make_advance(Decimal("300.00"), "30")
                SalesInvoice.objects.filter(pk=advance.pk).update(**{field: value})

                self.assertEqual(self.plan(), [])
                advance.delete()

    def test_a_cancelled_advance_is_not_offered(self):
        advance, _ = self.make_advance(Decimal("300.00"), "30")
        self.set_status(advance, SalesInvoiceStatus.CANCELLED)

        self.assertEqual(self.plan(), [])

    def test_an_advance_line_the_plan_does_not_offer_is_refused(self):
        _, line = self.make_advance(Decimal("300.00"), "30")
        invoice = self.goods(Decimal("0.5"))
        _, proforma_line = self.make_advance(Decimal("100.00"), "10", status=SalesInvoiceStatus.PROFORMA)

        for amounts in (
            {proforma_line.pk: Decimal("10.00")},
            {line.pk + 1000: Decimal("10.00")},
            {"not-a-pk": Decimal("10.00")},
        ):
            with self.subTest(amounts=amounts), self.assertRaises(ValidationError):
                AdvanceDeductionService.apply(invoice, amounts)
        self.assertEqual(AdvanceDeductionService.deduction_lines(invoice), [])

    def make_company(self):
        return Company.objects.create(name="Otra SL")


@override_settings(AEAT_SUBMISSIONS_ENABLED=False)
class OtherPartiesGoodsTests(_DeductionTestBase):
    """Goods of an expediente with a FINAL advance pending, billed by
    another company or to another client: the invoice cannot deduct the
    advance, so it is refused rather than billing those goods twice."""

    def setUp(self):
        super().setUp()
        self.make_advance(Decimal("300.00"), "30")

    def _invoice(self, company=None, client=None, ratio=Decimal("1")):
        return ProjectFileInvoiceService.create_invoice_from_project_file(
            project_file=self.project_file, company=company or self.company,
            client=client or self.client_obj, date_issued=date(2026, 9, 29), due_date=None,
            items_data=ProjectFileInvoiceService.build_items_for_ratio(self.summary(), ratio),
        )

    def test_another_company_is_refused(self):
        invoice = self._invoice(company=Company.objects.create(name="Otra SL"))

        with self.assertRaises(ValidationError):
            AdvanceDeductionService.apply(invoice)

    def test_another_client_is_refused(self):
        invoice = self._invoice(client=Client.objects.create(name="Otro", company=self.company))

        with self.assertRaises(ValidationError):
            AdvanceDeductionService.apply(invoice)

    def test_finalize_refuses_a_pro_forma_moved_to_another_client(self):
        # A partial invoice may leave the advance for later
        invoice = self._invoice(ratio=Decimal("0.5"))
        self.assertEqual(AdvanceDeductionService.apply(invoice, {}), [])
        SalesInvoice.objects.filter(pk=invoice.pk).update(
            client=Client.objects.create(name="Otro", company=self.company),
        )
        invoice.refresh_from_db()

        with self.assertRaises(ValidationError):
            SalesInvoiceService.finalize_invoice(invoice, send_email=False)
        invoice.refresh_from_db()
        self.assertEqual(invoice.status, SalesInvoiceStatus.PROFORMA)

    def test_the_wizard_keeps_the_expedientes_company(self):
        # Goods and advance invoices alike: another company is refused on
        # the company field, the expediente's own goes through.
        other = Company.objects.create(name="Otra SL")
        summary = self.summary()
        for mode, percentage in (("percentage", "100"), ("advance", "30")):
            with self.subTest(invoice_mode=mode):
                data = {
                    "company": other.pk,
                    "date_issued": "2026-09-29",
                    "invoice_mode": mode,
                    "percentage": percentage,
                    "payment_method": PaymentMethod.TRANSFER,
                }

                form = ProjectFileInvoiceSetupForm(
                    data=data, project_file=self.project_file, invoicing_summary=summary,
                )
                self.assertFalse(form.is_valid())
                self.assertEqual(list(form.errors), ["company"])

                form = ProjectFileInvoiceSetupForm(
                    data={**data, "company": self.company.pk}, project_file=self.project_file,
                    invoicing_summary=summary,
                )
                self.assertTrue(form.is_valid(), form.errors)


class CapTests(_DeductionTestBase):
    def test_an_expediente_that_shrank_leaves_the_rest_pending(self):
        self.make_advance(Decimal("800.00"), "80")
        Configuration.objects.filter(pk=self.config.pk).update(
            subtotal_price=Decimal("500.00"), total_price=Decimal("605.00"),
        )
        self.quote.recalc_totals()
        invoice = self.goods()

        [deduction] = AdvanceDeductionService.apply(invoice)

        # No tax base, taxable base or total below zero
        self.assertEqual(deduction.line_total.amount, Decimal("-500.00"))
        self.assertEqual(totals(invoice)[-1], Decimal("0.00"))
        self.assertEqual(self.summary()["advances_open_net"], Decimal("300.00"))
        self.project_file.refresh_from_db()
        self.assertEqual(self.project_file.advance_pending_amount.amount, Decimal("300.00"))


class UntaxedAdvanceLineTests(_DeductionTestBase):
    def test_is_not_taken_off_taxed_goods(self):
        """An untaxed part of an advance (untaxed goods of the expediente)
        only lowers untaxed goods: here they were invoiced before it, so it
        stays pending rather than lowering the IVA goods' taxable base
        without their IVA base."""
        untaxed = self.add_config(self.quote, Decimal("200.00"))
        AppliedTax.objects.filter(
            content_type=ContentType.objects.get_for_model(Configuration), object_id=untaxed.pk,
        ).delete()
        ProjectFileInvoiceService.create_invoice_from_project_file(
            project_file=self.project_file, company=self.company, client=self.client_obj,
            date_issued=date(2026, 9, 29), due_date=None,
            items_data=[{
                "source_id": f"config_{untaxed.pk}", "description": untaxed.name,
                "quantity": Decimal("1"), "unit_price": Decimal("200.00"),
                "discount_percentage": Decimal("0.00"),
            }],
        )
        advance, line = self.make_advance(Decimal("300.00"), "30", status=SalesInvoiceStatus.PROFORMA)
        SalesInvoiceItem.objects.create(
            invoice=advance, description="Anticipo (sin impuestos)", quantity=Decimal("1"),
            unit_price=Money(Decimal("60.00"), "EUR"),
            source_id=f"{ADVANCE_LINE_PREFIX}{self.project_file.pk}",
        )
        advance.recalculate_totals()
        self.set_status(advance, SalesInvoiceStatus.FINAL)
        rest = ProjectFileInvoiceService.create_invoice_from_project_file(
            project_file=self.project_file, company=self.company, client=self.client_obj,
            date_issued=date(2026, 9, 29), due_date=None,
            items_data=[{
                "source_id": f"config_{self.config.pk}", "description": self.config.name,
                "quantity": Decimal("1"), "unit_price": Decimal("1000.00"),
                "discount_percentage": Decimal("0.00"),
            }],
        )

        [deduction] = AdvanceDeductionService.apply(rest)

        self.assertEqual((deduction.advance_line, deduction.line_total.amount), (line, Decimal("-300.00")))
        self.assertEqual(self.bases(rest), {("IVA", Decimal("21.00")): Decimal("700.00")})
        self.assertEqual(totals(rest)[2], Decimal("700.00"))
        self.assertEqual(self.summary()["advances_open_net"], Decimal("60.00"))


class RoomTests(TestCase):
    def setUp(self):
        self.iva, _created = TaxType.objects.get_or_create(
            tax_name="IVA", country="ES", defaults={"behavior": "add"},
        )
        self.signature = frozenset({(self.iva, Decimal("10.00"))})

    def _room(self, base, groups=None):
        return _Room(InvoiceGoods(
            groups=groups or {},
            taxes={(self.iva.pk, Decimal("10.00")): (self.iva, Decimal("10.00"), base)},
            taxable_base=base,
        ))

    def test_every_cent_the_total_allows(self):
        # The slope estimate rounds 344.42 down to 344.41
        room = self._room(Decimal("344.42"))

        self.assertEqual(room.largest(self.signature, Decimal("344.43")), Decimal("344.42"))
        self.assertEqual(room.total(self.signature, Decimal("344.42")), Decimal("0.00"))

    def test_no_untaxed_goods_no_untaxed_deduction(self):
        room = self._room(Decimal("1000.00"), groups={1: {self.signature: Decimal("1000.00")}})
        self.assertEqual(room.largest(frozenset(), Decimal("60.00")), Decimal("0.00"))

        room = self._room(Decimal("1000.00"), groups={1: {frozenset(): Decimal("40.00")}})
        self.assertEqual(room.largest(frozenset(), Decimal("60.00")), Decimal("40.00"))
        room.take(frozenset(), Decimal("40.00"))
        self.assertEqual(room.largest(frozenset(), Decimal("20.00")), Decimal("0.00"))


@override_settings(AEAT_SUBMISSIONS_ENABLED=False)
class UserExpedienteFullAdvanceTests(UserExpedienteMixin, TestCase):
    def test_the_rest_invoice_takes_all_of_it(self):
        SalesInvoiceService.finalize_invoice(
            AdvanceInvoiceService.create(
                self.project_file, date_issued=date(2026, 9, 3), percentage=Decimal("100"),
            ),
            send_email=False,
        )
        rest = self.rest_invoice()

        self.assertEqual(totals(rest)[2:], (Decimal("0.00"),) * 3)
        self.assertEqual(
            ProjectFileInvoiceService.get_invoiceable_summary(self.project_file)["advances_open_net"],
            Decimal("0.00"),
        )


@override_settings(AEAT_SUBMISSIONS_ENABLED=False)
class RoundingDriftTests(AdvanceExpedienteMixin, TestCase):
    """1,000.00 at IVA 21 %, 333.33 and 77.77 at IVA 10 %, a 5 % discount
    (net base 1,340.54). Each VAT group's figures drift by a few cents over
    percentage invoices; the invoice that finishes the goods still takes
    every cent of the advance left."""

    def setUp(self):
        super().setUp()
        policy = DiscountPolicy.objects.create(name="Descuento", company=self.company)
        DiscountRule.objects.create(
            policy=policy, name="Descuento 5%", order=0, percentage=Decimal("5.00"),
            mode=DiscountMode.NET, level=DiscountLevel.BASKET, kind=DiscountKind.STANDARD,
        )
        self.quote.discount_policies.add(policy)
        self.add_config(self.quote, Decimal("333.33"), percentage=Decimal("10"))
        self.add_config(self.quote, Decimal("77.77"), percentage=Decimal("10"))

    def _invoice(self, percentage):
        """ "``percentage`` % del pendiente", its default deductions, issued."""
        summary = self.summary()
        ratio = ProjectFileInvoiceService.compute_invoice_line_ratio(
            summary, "percentage", Decimal(percentage),
        )
        invoice = ProjectFileInvoiceService.create_invoice_from_project_file(
            project_file=self.project_file, company=self.company, client=self.client_obj,
            date_issued=date(2026, 9, 29), due_date=None,
            items_data=ProjectFileInvoiceService.build_items_for_ratio(summary, ratio),
        )
        AdvanceDeductionService.apply(invoice)
        return SalesInvoiceService.finalize_invoice(invoice, send_email=False)

    def test_nothing_of_the_advance_is_left(self):
        advance = SalesInvoiceService.finalize_invoice(
            AdvanceInvoiceService.create(
                self.project_file, date_issued=date(2026, 9, 3), percentage=Decimal("90"),
            ),
            send_email=False,
        )

        invoices = [advance] + [self._invoice(percentage) for percentage in ("33", "50", "100")]

        summary = self.summary()
        self.assertFalse(summary["can_invoice"])
        self.assertEqual(summary["advances_open_net"], Decimal("0.00"))
        self.assertEqual(
            sum((invoice.taxable_base.amount for invoice in invoices), Decimal("0.00")),
            summary["total_invoiceable_subtotal_net"],
        )


class StoredTextLanguageTests(_DeductionTestBase):
    """The advance and deduction lines are stored in the documents'
    language, whatever language the operator works in."""

    def _advance_texts(self, language):
        with translation.override(language):
            advance = AdvanceInvoiceService.create(
                self.project_file, date_issued=date(2026, 9, 1), percentage=Decimal("30"),
            )
        texts = list(advance.items.values_list("description", flat=True))
        return advance, texts

    def test_advance_lines(self):
        advance, expected = self._advance_texts(settings.LANGUAGE_CODE)
        advance.delete()

        _advance, texts = self._advance_texts("en")

        self.assertEqual(texts, expected)

    def test_deduction_lines(self):
        advance, _texts = self._advance_texts(settings.LANGUAGE_CODE)
        self.set_status(advance, SalesInvoiceStatus.FINAL)
        invoice = self.goods()
        with translation.override(settings.LANGUAGE_CODE):
            expected = [line.description for line in AdvanceDeductionService.apply(invoice)]
        AdvanceDeductionService.remove(invoice)

        with translation.override("en"):
            texts = [line.description for line in AdvanceDeductionService.apply(invoice)]

        self.assertEqual(texts, expected)


class ApplyGuardTests(_DeductionTestBase):
    def test_only_on_a_pro_forma_goods_invoice(self):
        invoice = self.goods(Decimal("0.5"))
        advance, _ = self.make_advance(Decimal("300.00"), "30", status=SalesInvoiceStatus.PROFORMA)
        with self.assertRaises(ValidationError):
            AdvanceDeductionService.apply(advance)

        self.set_status(invoice, SalesInvoiceStatus.FINAL)
        with self.assertRaises(ValidationError):
            AdvanceDeductionService.apply(invoice)

    def test_nothing_pending_prices_nothing(self):
        invoice = self.goods()

        with mock.patch.object(ProjectFileInvoiceService, "get_invoiceable_summary") as summary:
            self.assertEqual(AdvanceDeductionService.apply(invoice), [])
        summary.assert_not_called()

    def test_not_twice(self):
        self.make_advance(Decimal("300.00"), "30")
        invoice = self.goods()
        AdvanceDeductionService.apply(invoice)

        with self.assertRaises(ValidationError):
            AdvanceDeductionService.apply(invoice)
        self.assertEqual(len(AdvanceDeductionService.deduction_lines(invoice)), 1)


    def test_nothing_below_zero(self):
        invoice = self.goods()
        AppliedTax.objects.filter(
            content_type=ContentType.objects.get_for_model(SalesInvoice), object_id=invoice.pk,
        ).update(base_amount=Decimal("-0.01"))
        with self.assertRaises(ValidationError):
            AdvanceDeductionService.check_totals(invoice)

        SalesInvoice.objects.filter(pk=invoice.pk).update(total_amount=Decimal("-1.00"))
        AppliedTax.objects.filter(
            content_type=ContentType.objects.get_for_model(SalesInvoice), object_id=invoice.pk,
        ).update(base_amount=Decimal("0.00"))
        invoice.refresh_from_db()
        with self.assertRaises(ValidationError):
            AdvanceDeductionService.check_totals(invoice)

        invoice = self.goods(Decimal("0"))
        AdvanceDeductionService.check_totals(invoice)


class RemoveAndReapplyTests(_DeductionTestBase):
    def setUp(self):
        super().setUp()
        self.make_advance(Decimal("300.00"), "30")
        self.invoice = self.goods()
        AdvanceDeductionService.apply(self.invoice)

    def test_remove_restores_the_tax_bases(self):
        restored = AdvanceDeductionService.remove(self.invoice)

        self.assertEqual(restored, Decimal("300.00"))
        self.assertEqual(AdvanceDeductionService.deduction_lines(self.invoice), [])
        self.assertEqual(self.bases(self.invoice), {("IVA", Decimal("21.00")): Decimal("1000.00")})
        self.assertEqual(totals(self.invoice)[-1], Decimal("1210.00"))
        self.project_file.refresh_from_db()
        self.assertEqual(self.project_file.advance_pending_amount.amount, Decimal("300.00"))

        AdvanceDeductionService.apply(self.invoice)
        self.assertEqual(self.bases(self.invoice), {("IVA", Decimal("21.00")): Decimal("700.00")})
        self.assertEqual(totals(self.invoice)[-1], Decimal("847.00"))

    def test_only_on_a_pro_forma(self):
        self.set_status(self.invoice, SalesInvoiceStatus.FINAL)
        with self.assertRaises(ValidationError):
            AdvanceDeductionService.remove(self.invoice)

    def _url(self, name):
        return reverse(f"admin:invoices_salesinvoice_{name}_detail_action", args=[self.invoice.pk])

    def _action(self, name, user):
        """The action's dialog, submitted (htmx posts it)."""
        self.client.force_login(user)
        return self.client.post(self._url(name), {"_form_submitted": "True"})

    def test_admin_actions(self):
        superuser = User.objects.create_superuser("advances-admin", "a@x.com", "pw")

        response = self._action("remove_advances", superuser)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response["HX-Redirect"], reverse("admin:invoices_salesinvoice_change", args=[self.invoice.pk]),
        )
        self.assertEqual(self.bases(self.invoice), {("IVA", Decimal("21.00")): Decimal("1000.00")})

        response = self._action("reapply_advances", superuser)
        self.assertEqual(self.bases(self.invoice), {("IVA", Decimal("21.00")): Decimal("700.00")})
        self.assertEqual(
            [m.level for m in get_messages(response.wsgi_request)], [messages.INFO] * 2,
        )

    def test_admin_actions_need_the_change_permission(self):
        viewer = User.objects.create_user("viewer", password="pw", is_staff=True)
        viewer.user_permissions.add(Permission.objects.get(codename="view_salesinvoice"))

        for name in ("remove_advances", "reapply_advances"):
            with self.subTest(action=name):
                self.assertEqual(self._action(name, viewer).status_code, 403)
        self.assertEqual(len(AdvanceDeductionService.deduction_lines(self.invoice)), 1)

    def test_a_plain_link_only_opens_the_dialog(self):
        # A GET (a link, maybe from another site) changes nothing
        self.client.force_login(User.objects.create_superuser("advances-admin", "a@x.com", "pw"))

        for name in ("remove_advances", "reapply_advances"):
            with self.subTest(action=name):
                response = self.client.get(self._url(name))
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, 'name="_form_submitted"')
        self.assertEqual(len(AdvanceDeductionService.deduction_lines(self.invoice)), 1)
        self.assertEqual(self.bases(self.invoice), {("IVA", Decimal("21.00")): Decimal("700.00")})


def _inline_post_data(formset):
    """What the browser posts for ``formset`` left as rendered."""
    data = {
        f"{formset.prefix}-{name}": value
        for name, value in formset.management_form.initial.items()
    }
    for form in formset.forms:
        for field in form:
            value = field.value()
            widget = field.field.widget
            if isinstance(widget, MultiWidget):
                parts = value if isinstance(value, (list, tuple)) else widget.decompress(value)
                for suffix, part in zip(widget.widgets_names, parts):
                    data[f"{field.html_name}{suffix}"] = "" if part is None else part
            elif value is not None and value is not False:
                data[field.html_name] = value
    return data


class ProtectedInlineTests(_DeductionTestBase):
    def setUp(self):
        super().setUp()
        self.make_advance(Decimal("300.00"), "30")
        self.invoice = self.goods()
        AdvanceDeductionService.apply(self.invoice)
        self.request = RequestFactory().get("/")
        self.request.user = User.objects.create_superuser("inline-admin", "i@x.com", "pw")

    def _formset(self, change=None):
        inline = SalesInvoiceItemInline(SalesInvoice, admin.site)
        FormSet = inline.get_formset(self.request, self.invoice)
        data = _inline_post_data(FormSet(instance=self.invoice, prefix="items"))
        index = next(
            i for i, line in enumerate(FormSet(instance=self.invoice, prefix="items").get_queryset())
            if line.advance_line_id
        )
        if change:
            change(data, f"items-{index}-")
        return FormSet(data, instance=self.invoice, prefix="items")

    def test_unchanged_rows_pass(self):
        self.assertTrue(self._formset().is_valid())

    def test_deleting_a_deduction_row_is_refused(self):
        formset = self._formset(lambda data, prefix: data.update({f"{prefix}DELETE": "on"}))
        self.assertFalse(formset.is_valid())
        self.assertTrue(formset.non_form_errors())

    def test_editing_a_deduction_row_is_refused(self):
        formset = self._formset(lambda data, prefix: data.update({f"{prefix}description": "Otra"}))
        self.assertFalse(formset.is_valid())
        self.assertTrue(formset.non_form_errors())

    def test_editing_a_goods_row_is_allowed(self):
        def change(data, _prefix):
            goods = next(
                key for key, value in data.items()
                if key.endswith("-description") and "Configuración" in str(value)
            )
            data[goods] = "Ventana corredera"

        self.assertTrue(self._formset(change).is_valid())

    def test_tax_bases_are_read_only_on_invoices_with_advance_rows(self):
        inline = AppliedTaxInline(SalesInvoice, admin.site)

        self.assertIn("base_amount", inline.get_readonly_fields(self.request, self.invoice))
        advance = SalesInvoiceItem.objects.get(invoice__document_type="advance").invoice
        self.assertIn("base_amount", inline.get_readonly_fields(self.request, advance))
        plain = SalesInvoice.objects.create(company=self.company, client=self.client_obj)
        self.assertNotIn("base_amount", inline.get_readonly_fields(self.request, plain))

    def test_an_advance_takes_no_new_lines_and_keeps_its_taxes(self):
        # A line added to it would be billed untaxed and deducted as a group of its own
        items = SalesInvoiceItemInline(SalesInvoice, admin.site)
        taxes = AppliedTaxInline(SalesInvoice, admin.site)
        advance, _ = self.make_advance(Decimal("100.00"), "10", status=SalesInvoiceStatus.PROFORMA)
        plain = SalesInvoice.objects.create(company=self.company, client=self.client_obj)

        for invoice, allowed in ((advance, False), (plain, True), (self.invoice, True)):
            with self.subTest(invoice=invoice.document_type, allowed=allowed):
                self.assertEqual(items.has_add_permission(self.request, invoice), allowed)
                self.assertEqual(taxes.has_add_permission(self.request, invoice), allowed)
                self.assertEqual(taxes.has_change_permission(self.request, invoice), allowed)
                self.assertEqual(taxes.has_delete_permission(self.request, invoice), allowed)


@override_settings(AEAT_SUBMISSIONS_ENABLED=False)
class WizardDeductionTests(_DeductionTestBase):
    STEP = WIZARD_STEP

    def setUp(self):
        super().setUp()
        _, self.line = self.make_advance(Decimal("300.00"), "30")
        self.url = self.login_to_wizard()

    def _to_deductions(self, percentage="100"):
        self.client.get(self.url)
        response = self.client.post(self.url, self.wizard_setup_data(percentage=percentage))
        self.assertEqual(response.context["wizard"]["steps"].current, "items")
        formset = response.context["wizard"]["form"]
        data = {
            self.STEP: "items",
            # As rendered: the built rows are the formset's initial forms
            "items-TOTAL_FORMS": str(len(formset.forms)), "items-INITIAL_FORMS": str(len(formset.forms)),
            "items-MIN_NUM_FORMS": "0", "items-MAX_NUM_FORMS": "1000",
        }
        for index, form in enumerate(formset.forms):
            for name in ("source_id", "description", "quantity", "unit_price", "discount_percentage"):
                data[f"items-{index}-{name}"] = form.initial.get(name, "")
        return self.client.post(self.url, data)

    def _post_deductions(self, *rows):
        return self.client.post(self.url, {
            self.STEP: "deductions",
            "deductions-TOTAL_FORMS": str(len(rows)), "deductions-INITIAL_FORMS": str(len(rows)),
            "deductions-MIN_NUM_FORMS": "0", "deductions-MAX_NUM_FORMS": "1000",
            **{
                f"deductions-{index}-{name}": value
                for index, row in enumerate(rows)
                for name, value in row.items()
            },
        })

    def _finish(self):
        return self.client.post(self.url, {self.STEP: "notes", "notes-notes": ""})

    def test_setup_step_lists_the_advances_pending(self):
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.line.invoice.display_number)

    def test_closing_invoice_deducts_the_advance(self):
        response = self._to_deductions()

        wizard = response.context["wizard"]
        self.assertEqual(wizard["steps"].current, "deductions")
        self.assertEqual(wizard["steps"].count, 4)
        [form] = wizard["form"].forms
        self.assertEqual(form.row.line, self.line)
        self.assertTrue(form.row.fixed)
        self.assertEqual(form.initial, {"advance_line": self.line.pk, "amount": Decimal("300.00")})

        response = self._post_deductions({"advance_line": self.line.pk, "amount": "300.00"})
        self.assertEqual(response.context["wizard"]["steps"].current, "notes")
        response = self._finish()

        invoice = SalesInvoice.objects.get(project_file=self.project_file, document_type="invoice")
        self.assertRedirects(
            response, reverse("admin:invoices_salesinvoice_change", args=[invoice.pk]),
            fetch_redirect_response=False,
        )
        # Goods from the items step; the deduction only from the deductions step
        [goods] = invoice.items.filter(advance_line__isnull=True)
        self.assertEqual(goods.source_id, f"config_{self.config.pk}")
        [deduction] = AdvanceDeductionService.deduction_lines(invoice)
        self.assertEqual((deduction.advance_line, deduction.line_total.amount), (self.line, Decimal("-300.00")))
        self.assertEqual(totals(invoice)[-1], Decimal("847.00"))

    def test_a_fixed_row_ignores_the_amount_posted(self):
        self._to_deductions()
        self._post_deductions({"advance_line": self.line.pk, "amount": "10.00"})
        self._finish()

        invoice = SalesInvoice.objects.get(project_file=self.project_file, document_type="invoice")
        [deduction] = AdvanceDeductionService.deduction_lines(invoice)
        self.assertEqual(deduction.line_total.amount, Decimal("-300.00"))

    def test_partial_invoice_takes_the_amount_entered(self):
        response = self._to_deductions(percentage="50")
        [form] = response.context["wizard"]["form"].forms
        self.assertEqual((form.row.minimum, form.row.amount), (Decimal("0.00"), Decimal("150.00")))

        response = self._post_deductions({"advance_line": self.line.pk, "amount": "400.00"})
        self.assertEqual(response.context["wizard"]["steps"].current, "deductions")
        self._post_deductions({"advance_line": self.line.pk, "amount": "100.00"})
        self._finish()

        invoice = SalesInvoice.objects.get(project_file=self.project_file, document_type="invoice")
        [deduction] = AdvanceDeductionService.deduction_lines(invoice)
        self.assertEqual(deduction.line_total.amount, Decimal("-100.00"))

    def test_a_tampered_advance_line_is_refused(self):
        _, cancelled_line = self.make_advance(Decimal("100.00"), "10", status=SalesInvoiceStatus.CANCELLED)
        self._to_deductions()

        for pk in (cancelled_line.pk, self.line.pk + 1000):
            with self.subTest(pk=pk):
                response = self._post_deductions({"advance_line": pk, "amount": "10.00"})
                self.assertEqual(response.context["wizard"]["steps"].current, "deductions")
                self.assertFalse(response.context["wizard"]["form"].is_valid())
        self.assertFalse(SalesInvoice.objects.filter(document_type="invoice").exists())


@override_settings(AEAT_SUBMISSIONS_ENABLED=False)
class ConfigurationInvoiceTests(_DeductionTestBase):
    def test_refused_while_advances_are_pending(self):
        self.make_advance(Decimal("300.00"), "30", status=SalesInvoiceStatus.PROFORMA)

        with self.assertRaises(ValidationError):
            SalesInvoiceService.create_from_configuration(self.config)
        self.assertFalse(SalesInvoice.objects.filter(configuration=self.config).exists())

    def test_production_admin_shows_the_refusal(self):
        self.make_advance(Decimal("300.00"), "30")
        ticket = ProductionTicket.objects.create(
            company=self.company,
            content_type=ContentType.objects.get_for_model(Configuration),
            object_id=self.config.pk,
        )
        self.client.force_login(User.objects.create_superuser("production", "p@x.com", "pw"))

        response = self.client.get(
            reverse("admin:production_productionticket_generate_invoice_action", args=[ticket.pk])
        )

        self.assertRedirects(
            response, reverse("admin:production_productionticket_changelist"),
            fetch_redirect_response=False,
        )
        self.assertEqual([m.level for m in get_messages(response.wsgi_request)], [messages.ERROR])
        self.assertFalse(SalesInvoice.objects.filter(configuration=self.config).exists())

    def test_allowed_without_advances(self):
        invoice = SalesInvoiceService.create_from_configuration(self.config)
        self.assertEqual(invoice.configuration, self.config)
