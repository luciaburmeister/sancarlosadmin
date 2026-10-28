from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse
from djmoney.money import Money

from company.models import Company
from inventory.models import InventoryItem
from invoices.models import Invoice, InvoiceItem, InvoicePOAllocation
from invoices.services.po_matching import POInvoiceMatchService
from materials.models import Provider
from purchasing.models import PurchaseOrder, PurchaseOrderItem, PurchaseOrderStatus


class POMatchingTestBase(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Test Company", tax_identifier="B12345678")
        self.provider = Provider.objects.create(company=self.company, name="Proveedor SA")
        self.other_provider = Provider.objects.create(company=self.company, name="Otro Proveedor")
        self.item_profile = InventoryItem.objects.create(name="Perfil aluminio", company=self.company)
        self.item_glass = InventoryItem.objects.create(name="Vidrio templado", company=self.company)

        self.po = self._make_po(self.provider)
        self.po_line_profile = self._make_po_line(self.po, self.item_profile, "10", "5.00")
        self.po_line_glass = self._make_po_line(self.po, self.item_glass, "4", "20.00")

        self.invoice = self._make_invoice("F-001")
        self.inv_line_profile = self._make_invoice_line(self.invoice, "Perfil aluminio 40x40", "10", "5.00")
        self.inv_line_glass = self._make_invoice_line(self.invoice, "Vidrio templado 4mm", "4", "20.00")

    def _make_po(self, provider, status=PurchaseOrderStatus.APPROVED):
        return PurchaseOrder.objects.create(
            company=self.company,
            provider=provider,
            date_ordered=date(2026, 6, 1),
            status=status,
        )

    def _make_po_line(self, po, inventory_item, qty, price):
        return PurchaseOrderItem.objects.create(
            purchase_order=po,
            inventory_item=inventory_item,
            quantity=Decimal(qty),
            unit_price=Money(price, "EUR"),
        )

    def _make_invoice(self, number, provider="default"):
        return Invoice.objects.create(
            company=self.company,
            provider=self.provider if provider == "default" else provider,
            invoice_number=number,
            date_issued=date(2026, 6, 20),
        )

    def _make_invoice_line(self, invoice, description, qty, price):
        return InvoiceItem.objects.create(
            invoice=invoice,
            description=description,
            quantity=Decimal(qty),
            unit_price=Money(price, "EUR"),
        )


class POInvoiceMatchServiceTest(POMatchingTestBase):
    def test_candidate_pos_filters_provider_status_and_pending(self):
        other_po = self._make_po(self.other_provider)
        self._make_po_line(other_po, self.item_profile, "5", "5.00")
        draft_po = self._make_po(self.provider, status=PurchaseOrderStatus.DRAFT)
        self._make_po_line(draft_po, self.item_profile, "5", "5.00")

        candidates = POInvoiceMatchService.candidate_pos_for_invoice(self.invoice)
        self.assertEqual([po.pk for po in candidates], [self.po.pk])

    def test_candidate_pos_excludes_fully_invoiced(self):
        InvoicePOAllocation.objects.create(
            invoice_item=self.inv_line_profile, po_item=self.po_line_profile, quantity=Decimal("10")
        )
        InvoicePOAllocation.objects.create(
            invoice_item=self.inv_line_glass, po_item=self.po_line_glass, quantity=Decimal("4")
        )
        candidates = POInvoiceMatchService.candidate_pos_for_invoice(self.invoice)
        self.assertEqual(candidates, [])

    def test_candidate_pos_excludes_dates_outside_window(self):
        old_po = PurchaseOrder.objects.create(
            company=self.company,
            provider=self.provider,
            date_ordered=date(2025, 6, 1),
            status=PurchaseOrderStatus.APPROVED,
        )
        self._make_po_line(old_po, self.item_profile, "5", "5.00")

        candidates = POInvoiceMatchService.candidate_pos_for_invoice(self.invoice)
        self.assertEqual([po.pk for po in candidates], [self.po.pk])

    def test_candidate_invoices_excludes_dates_outside_window_keeps_undated(self):
        old_invoice = Invoice.objects.create(
            company=self.company,
            provider=self.provider,
            invoice_number="F-OLD",
            date_issued=date(2025, 6, 20),
        )
        self._make_invoice_line(old_invoice, "Perfil viejo", "2", "5.00")
        undated_invoice = Invoice.objects.create(
            company=self.company,
            provider=self.provider,
            invoice_number="F-NODATE",
        )
        self._make_invoice_line(undated_invoice, "Pendiente OCR", "1", "1.00")

        pks = {inv.pk for inv in POInvoiceMatchService.candidate_invoices_for_po(self.po)}
        self.assertNotIn(old_invoice.pk, pks)
        self.assertIn(undated_invoice.pk, pks)
        self.assertIn(self.invoice.pk, pks)

    def test_candidate_invoices_for_po(self):
        matched_invoice = self._make_invoice("F-FULL")
        line = self._make_invoice_line(matched_invoice, "Perfil", "3", "5.00")
        InvoicePOAllocation.objects.create(
            invoice_item=line, po_item=self.po_line_profile, quantity=Decimal("3")
        )
        no_provider_invoice = self._make_invoice("F-OCR", provider=None)
        self._make_invoice_line(no_provider_invoice, "Algo", "1", "1.00")

        candidates = POInvoiceMatchService.candidate_invoices_for_po(self.po)
        pks = {inv.pk for inv in candidates}
        self.assertIn(self.invoice.pk, pks)
        self.assertIn(no_provider_invoice.pk, pks)
        self.assertNotIn(matched_invoice.pk, pks)

    def test_suggestions_pair_by_price_and_description(self):
        suggestions = POInvoiceMatchService.suggest_pairs_for_invoice(self.invoice, self.po)
        self.assertEqual(
            suggestions[self.inv_line_profile.pk],
            {"target_id": self.po_line_profile.pk, "quantity": Decimal("10")},
        )
        self.assertEqual(
            suggestions[self.inv_line_glass.pk],
            {"target_id": self.po_line_glass.pk, "quantity": Decimal("4")},
        )

    def test_suggestions_cap_at_pending_and_consume_it(self):
        # Two invoice lines competing for one PO line with qty 10
        solo_po = self._make_po(self.provider)
        solo_line = self._make_po_line(solo_po, self.item_profile, "10", "5.00")
        invoice = self._make_invoice("F-002")
        line_a = self._make_invoice_line(invoice, "Perfil aluminio", "8", "5.00")
        line_b = self._make_invoice_line(invoice, "Perfil aluminio", "8", "5.00")

        suggestions = POInvoiceMatchService.suggest_pairs_for_invoice(invoice, solo_po)
        self.assertEqual(suggestions[line_a.pk]["quantity"], Decimal("8"))
        # Second line only gets the remaining 2, not another 8
        self.assertEqual(suggestions[line_b.pk]["quantity"], Decimal("2"))
        self.assertEqual(suggestions[line_b.pk]["target_id"], solo_line.pk)

    def test_suggestions_skip_fully_allocated_invoice_lines(self):
        InvoicePOAllocation.objects.create(
            invoice_item=self.inv_line_profile, po_item=self.po_line_profile, quantity=Decimal("10")
        )
        suggestions = POInvoiceMatchService.suggest_pairs_for_invoice(self.invoice, self.po)
        self.assertNotIn(self.inv_line_profile.pk, suggestions)
        self.assertIn(self.inv_line_glass.pk, suggestions)

    def test_confirm_creates_allocations(self):
        created = POInvoiceMatchService.confirm([
            {"invoice_item": self.inv_line_profile, "po_item": self.po_line_profile, "quantity": Decimal("10")},
            {"invoice_item": self.inv_line_glass, "po_item": self.po_line_glass, "quantity": Decimal("4")},
        ])
        self.assertEqual(len(created), 2)
        self.po_line_profile.refresh_from_db()
        self.po_line_glass.refresh_from_db()
        self.assertEqual(self.po_line_profile.invoiced_quantity, Decimal("10"))
        self.assertEqual(self.po_line_glass.invoiced_quantity, Decimal("4"))

    def test_confirm_is_atomic_on_validation_error(self):
        with self.assertRaises(ValidationError):
            POInvoiceMatchService.confirm([
                {"invoice_item": self.inv_line_profile, "po_item": self.po_line_profile, "quantity": Decimal("5")},
                # Over-allocates the glass line (ordered 4)
                {"invoice_item": self.inv_line_glass, "po_item": self.po_line_glass, "quantity": Decimal("9")},
            ])
        self.assertEqual(InvoicePOAllocation.objects.count(), 0)
        self.po_line_profile.refresh_from_db()
        self.assertEqual(self.po_line_profile.invoiced_quantity, Decimal("0"))

    def test_candidate_invoices_for_pos_pools_several_pos(self):
        other_po = self._make_po(self.provider)
        self._make_po_line(other_po, self.item_glass, "4", "20.00")

        candidates = POInvoiceMatchService.candidate_invoices_for_pos([self.po, other_po])
        self.assertIn(self.invoice.pk, {inv.pk for inv in candidates})

    def test_suggest_pairs_for_pos_splits_one_invoice_line_across_pos(self):
        # Two POs each ordering 5 of the same item; one invoice line for 10.
        po_a = self._make_po(self.provider)
        line_a = self._make_po_line(po_a, self.item_profile, "5", "5.00")
        po_b = self._make_po(self.provider)
        line_b = self._make_po_line(po_b, self.item_profile, "5", "5.00")
        invoice = self._make_invoice("F-003")
        inv_line = self._make_invoice_line(invoice, "Perfil aluminio", "10", "5.00")

        suggestions = POInvoiceMatchService.suggest_pairs_for_pos([po_a, po_b], invoice)

        self.assertEqual(suggestions[line_a.pk], {"target_id": inv_line.pk, "quantity": Decimal("5")})
        self.assertEqual(suggestions[line_b.pk], {"target_id": inv_line.pk, "quantity": Decimal("5")})


class MatchViewsTest(POMatchingTestBase):
    def setUp(self):
        super().setUp()
        User = get_user_model()
        self.admin = User.objects.create_superuser("admin", "admin@test.com", "pass")
        self.client.force_login(self.admin)

    def test_invoice_side_candidate_page(self):
        url = reverse("admin:invoices_invoice_match_po", args=[self.invoice.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.po.number)

    def test_invoice_side_review_page_prefills(self):
        url = reverse("admin:invoices_invoice_match_po", args=[self.invoice.pk])
        response = self.client.get(f"{url}?po={self.po.pk}")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Perfil aluminio 40x40")
        self.assertContains(response, f'name="target_{self.inv_line_profile.pk}"')

    def test_invoice_side_post_creates_allocations(self):
        url = reverse("admin:invoices_invoice_match_po", args=[self.invoice.pk])
        response = self.client.post(f"{url}?po={self.po.pk}", {
            "po": self.po.pk,
            f"target_{self.inv_line_profile.pk}": self.po_line_profile.pk,
            f"qty_{self.inv_line_profile.pk}": "10",
            f"target_{self.inv_line_glass.pk}": "",
            f"qty_{self.inv_line_glass.pk}": "",
        })
        self.assertRedirects(
            response, reverse("admin:invoices_invoice_change", args=[self.invoice.pk])
        )
        self.po_line_profile.refresh_from_db()
        self.assertEqual(self.po_line_profile.invoiced_quantity, Decimal("10"))
        self.assertEqual(InvoicePOAllocation.objects.count(), 1)

    def test_po_side_candidate_page(self):
        url = reverse("admin:purchasing_purchaseorder_match_invoice", args=[self.po.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "F-001")

    def test_po_side_post_creates_allocations(self):
        url = reverse("admin:purchasing_purchaseorder_match_invoice", args=[self.po.pk])
        response = self.client.post(f"{url}?invoice={self.invoice.pk}", {
            "invoice": self.invoice.pk,
            f"target_{self.po_line_profile.pk}": self.inv_line_profile.pk,
            f"qty_{self.po_line_profile.pk}": "10",
            f"target_{self.po_line_glass.pk}": self.inv_line_glass.pk,
            f"qty_{self.po_line_glass.pk}": "4",
        })
        self.assertRedirects(
            response, reverse("admin:purchasing_purchaseorder_change", args=[self.po.pk])
        )
        self.assertEqual(InvoicePOAllocation.objects.count(), 2)

    def test_po_side_blocked_for_draft_po(self):
        draft_po = self._make_po(self.provider, status=PurchaseOrderStatus.DRAFT)
        url = reverse("admin:purchasing_purchaseorder_match_invoice", args=[draft_po.pk])
        response = self.client.get(url)
        self.assertRedirects(
            response,
            reverse("admin:purchasing_purchaseorder_change", args=[draft_po.pk]),
        )

    def test_invoice_side_post_validation_error_shows_message(self):
        url = reverse("admin:invoices_invoice_match_po", args=[self.invoice.pk])
        response = self.client.post(f"{url}?po={self.po.pk}", {
            "po": self.po.pk,
            f"target_{self.inv_line_glass.pk}": self.po_line_glass.pk,
            f"qty_{self.inv_line_glass.pk}": "9",  # over-allocates (ordered 4)
        }, follow=True)
        self.assertEqual(InvoicePOAllocation.objects.count(), 0)
        messages = [str(m) for m in response.context["messages"]]
        self.assertTrue(any("invoiced" in m or "facturad" in m for m in messages))


class BulkMatchViewsTest(POMatchingTestBase):
    def setUp(self):
        super().setUp()
        User = get_user_model()
        self.admin = User.objects.create_superuser("admin", "admin@test.com", "pass")
        self.client.force_login(self.admin)
        self.po_b = self._make_po(self.provider)
        self.po_b_line = self._make_po_line(self.po_b, self.item_glass, "4", "20.00")

    def test_changelist_action_redirects_to_bulk_matcher(self):
        url = reverse("admin:purchasing_purchaseorder_changelist")
        response = self.client.post(url, {
            "action": "match_selected_pos_to_invoice",
            "_selected_action": [self.po.pk, self.po_b.pk],
        })
        self.assertEqual(response.status_code, 302)
        location = response.url
        base, _, ids_param = location.partition("?ids=")
        self.assertEqual(base, reverse("admin:purchasing_purchaseorder_bulk_match_invoice"))
        self.assertEqual({int(v) for v in ids_param.split(",")}, {self.po.pk, self.po_b.pk})

    def test_changelist_action_skips_non_matchable_statuses(self):
        draft_po = self._make_po(self.provider, status=PurchaseOrderStatus.DRAFT)
        url = reverse("admin:purchasing_purchaseorder_changelist")
        response = self.client.post(url, {
            "action": "match_selected_pos_to_invoice",
            "_selected_action": [self.po.pk, draft_po.pk],
        }, follow=True)
        self.assertRedirects(
            response,
            reverse("admin:purchasing_purchaseorder_bulk_match_invoice") + f"?ids={self.po.pk}",
        )
        messages = [str(m) for m in response.context["messages"]]
        self.assertTrue(any("skipped" in m or "omitid" in m for m in messages))

    def test_bulk_candidate_page_shows_pooled_pos(self):
        url = reverse("admin:purchasing_purchaseorder_bulk_match_invoice")
        response = self.client.get(f"{url}?ids={self.po.pk},{self.po_b.pk}")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.po.number)
        self.assertContains(response, self.po_b.number)
        self.assertContains(response, "F-001")

    def test_bulk_post_creates_allocations_across_pos(self):
        url = reverse("admin:purchasing_purchaseorder_bulk_match_invoice")
        response = self.client.post(f"{url}?ids={self.po.pk},{self.po_b.pk}&invoice={self.invoice.pk}", {
            "ids": f"{self.po.pk},{self.po_b.pk}",
            "invoice": self.invoice.pk,
            f"target_{self.po_line_profile.pk}": self.inv_line_profile.pk,
            f"qty_{self.po_line_profile.pk}": "10",
            f"target_{self.po_line_glass.pk}": "",
            f"qty_{self.po_line_glass.pk}": "",
            f"target_{self.po_b_line.pk}": self.inv_line_glass.pk,
            f"qty_{self.po_b_line.pk}": "4",
        })
        self.assertRedirects(response, reverse("admin:purchasing_purchaseorder_changelist"))
        self.assertEqual(InvoicePOAllocation.objects.count(), 2)
        self.po_line_profile.refresh_from_db()
        self.po_b_line.refresh_from_db()
        self.assertEqual(self.po_line_profile.invoiced_quantity, Decimal("10"))
        self.assertEqual(self.po_b_line.invoiced_quantity, Decimal("4"))

    def test_bulk_view_rejects_mixed_providers(self):
        other_po = self._make_po(self.other_provider)
        self._make_po_line(other_po, self.item_profile, "1", "1.00")
        url = reverse("admin:purchasing_purchaseorder_bulk_match_invoice")
        response = self.client.get(f"{url}?ids={self.po.pk},{other_po.pk}", follow=True)
        self.assertRedirects(response, reverse("admin:purchasing_purchaseorder_changelist"))
        messages = [str(m) for m in response.context["messages"]]
        self.assertTrue(any("provider" in m or "proveedor" in m for m in messages))

    def test_bulk_view_errors_without_ids(self):
        url = reverse("admin:purchasing_purchaseorder_bulk_match_invoice")
        response = self.client.get(url, follow=True)
        self.assertRedirects(response, reverse("admin:purchasing_purchaseorder_changelist"))
