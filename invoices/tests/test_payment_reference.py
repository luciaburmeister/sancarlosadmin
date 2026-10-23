from datetime import date
from unittest import mock

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, TestCase
from djmoney.money import Money

from invoices import payment_reference as payment_references
from invoices.models import Invoice, SalesInvoice
from invoices.services.sales_invoice_service import SalesInvoiceService
from invoices.tests.factories import make_client, make_company
from materials.models import Provider


def ref_for(body):
    return f"RF{payment_references._check_digits(body)}{body}"


REF = ref_for("7K3QX9MZ")


class PaymentReferenceFormatTests(SimpleTestCase):
    def test_generated_references_are_valid(self):
        for _ in range(200):
            ref = payment_references.generate()
            self.assertRegex(ref, payment_references.REFERENCE_RE)
            self.assertEqual(len(ref), payment_references.LENGTH)
            self.assertTrue(payment_references.is_valid(ref))

    def test_digit_for_digit_or_letter_for_letter_typo_is_rejected(self):
        for i in range(2, len(REF)):
            for char in payment_references.ALPHABET:
                if char != REF[i] and char.isdigit() == REF[i].isdigit():
                    typo = REF[:i] + char + REF[i + 1:]
                    self.assertFalse(payment_references.is_valid(typo), typo)

    def test_almost_every_typo_or_swap_is_rejected(self):
        # A letter typed for a digit (or vice versa) changes how many digits
        # mod 97 sees, so ~0.5% slip through; they would still have to equal
        # another open reference of the same company to match anything.
        typos = accepted = 0
        for _ in range(300):
            ref = payment_references.generate()
            for i in range(4, len(ref)):
                variants = [ref[:i] + c + ref[i + 1:] for c in payment_references.ALPHABET if c != ref[i]]
                if i < len(ref) - 1 and ref[i] != ref[i + 1]:
                    variants.append(ref[:i] + ref[i + 1] + ref[i] + ref[i + 2:])
                typos += len(variants)
                accepted += sum(payment_references.is_valid(v) for v in variants)
        self.assertLess(accepted / typos, 0.01)

    def test_display_groups_by_four(self):
        self.assertEqual(payment_references.display(REF), f"{REF[:4]} {REF[4:8]} {REF[8:]}")
        self.assertEqual(payment_references.display(None), "")

    def test_found_however_the_payer_writes_it(self):
        spaced = payment_references.display(REF)
        for concept in (
            REF,
            spaced,
            spaced.lower(),
            spaced.replace(" ", "-"),
            f"PAGO FRA {spaced} GRACIAS",
            f"TRANSFERENCIA/{REF}.",
        ):
            self.assertEqual(payment_references.find_in_text(concept), [REF], concept)

    def test_found_when_a_false_window_overlaps_it(self):
        self.assertEqual(payment_references.find_in_text(f"RF12 FRA {REF}"), [REF])

    def test_letters_typed_for_digits_are_corrected(self):
        ref = ref_for("10K3QX9M")
        typed = ref[:4] + "IOK3QX9M"
        self.assertEqual(payment_references.find_in_text(typed), [ref])

    def test_bad_checksum_and_missing_reference_find_nothing(self):
        wrong = REF[:2] + f"{(int(REF[2:4]) + 1) % 100:02d}" + REF[4:]
        self.assertEqual(payment_references.find_in_text(wrong), [])
        self.assertEqual(payment_references.find_in_text("PAGO FACTURA 2026-0042"), [])
        self.assertEqual(payment_references.find_in_text(None), [])

    def test_several_references_keep_their_order_once(self):
        other = ref_for("ABCDEFGH")
        self.assertEqual(
            payment_references.find_in_text(f"{other} {REF} {other}"), [other, REF],
        )


class PaymentReferenceAssignmentTests(TestCase):
    def setUp(self):
        self.company = make_company()
        self.client_obj = make_client(company=self.company)
        self.provider = Provider.objects.create(company=self.company, name="ACME")

    def _sales_invoice(self, company=None, **fields):
        company = company or self.company
        return SalesInvoice.objects.create(
            company=company, client=make_client(company=company),
            date_issued=date(2026, 1, 1), total_amount=Money("100.00", "EUR"), **fields,
        )

    def _invoice(self, company=None, **fields):
        return Invoice.objects.create(
            company=company or self.company, provider=self.provider,
            date_issued=date(2026, 1, 1), total_amount=Money("100.00", "EUR"),
            file=SimpleUploadedFile("invoice.pdf", b"dummy", content_type="application/pdf"),
            **fields,
        )

    def test_new_invoices_get_a_reference_that_never_changes(self):
        sales = self._sales_invoice()
        purchase = self._invoice()
        self.assertTrue(payment_references.is_valid(sales.payment_reference))
        self.assertTrue(payment_references.is_valid(purchase.payment_reference))

        before = sales.payment_reference
        sales.save()
        sales.refresh_from_db()
        self.assertEqual(sales.payment_reference, before)

    def test_external_sales_invoice_and_companyless_invoice_get_none(self):
        self.assertIsNone(self._sales_invoice(is_external=True).payment_reference)
        self.assertIsNone(
            Invoice.objects.create(
                provider=self.provider, total_amount=Money("1.00", "EUR"),
                file=SimpleUploadedFile("invoice.pdf", b"dummy", content_type="application/pdf"),
            ).payment_reference
        )

    def test_duplicate_gets_its_own_reference(self):
        original = self._sales_invoice()
        copy = SalesInvoiceService.duplicate_invoice(original)
        self.assertTrue(copy.payment_reference)
        self.assertNotEqual(copy.payment_reference, original.payment_reference)

    def test_same_reference_allowed_in_another_company(self):
        other_company = make_company(name="Other Co")
        with mock.patch.object(payment_references, "generate", return_value=REF):
            first = self._sales_invoice()
            second = self._sales_invoice(company=other_company)
        self.assertEqual(first.payment_reference, REF)
        self.assertEqual(second.payment_reference, REF)

    def test_taken_reference_is_regenerated_across_both_tables(self):
        other = ref_for("ABCDEFGH")
        with mock.patch.object(payment_references, "generate", side_effect=[REF, REF, other]):
            purchase = self._invoice()
            sales = self._sales_invoice()
        self.assertEqual(purchase.payment_reference, REF)
        self.assertEqual(sales.payment_reference, other)


class PaymentReferenceBackfillTests(TestCase):
    def test_backfill_fills_existing_invoices_unique_per_company(self):
        from importlib import import_module

        from django.apps import apps

        backfill = import_module("invoices.migrations.0105_backfill_payment_reference").backfill_payment_references
        company = make_company()
        other_company = make_company(name="Other Co")
        provider = Provider.objects.create(company=company, name="ACME")
        kept = SalesInvoice.objects.create(
            company=company, client=make_client(company=company), total_amount=Money("1.00", "EUR"),
        )
        sales = [
            SalesInvoice.objects.create(
                company=c, client=make_client(company=c), total_amount=Money("1.00", "EUR"),
            )
            for c in (company, company, other_company)
        ]
        external = SalesInvoice.objects.create(
            company=company, client=make_client(company=company), total_amount=Money("1.00", "EUR"),
            is_external=True,
        )
        purchase = Invoice.objects.create(
            company=company, provider=provider, total_amount=Money("1.00", "EUR"),
            file=SimpleUploadedFile("invoice.pdf", b"dummy", content_type="application/pdf"),
        )
        SalesInvoice.objects.filter(pk__in=[s.pk for s in sales]).update(payment_reference=None)
        Invoice.objects.filter(pk=purchase.pk).update(payment_reference=None)
        kept_ref = kept.payment_reference

        backfill(apps, None)

        kept.refresh_from_db()
        self.assertEqual(kept.payment_reference, kept_ref)
        refs = [kept_ref]
        for invoice in (*sales, purchase):
            invoice.refresh_from_db()
            self.assertTrue(payment_references.is_valid(invoice.payment_reference))
            refs.append(invoice.payment_reference)
        self.assertEqual(len(set(refs)), len(refs))
        external.refresh_from_db()
        self.assertIsNone(external.payment_reference)
