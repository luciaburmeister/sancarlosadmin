from datetime import date, timedelta
from decimal import Decimal
import time
from unittest.mock import patch

from django.contrib import admin
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.contrib.contenttypes.models import ContentType
from django.core.cache import cache
from django.contrib.messages.middleware import MessageMiddleware
from django.contrib.sessions.middleware import SessionMiddleware
from django.core import mail
from django.core.exceptions import ValidationError
from django.http import Http404
from django.test import RequestFactory, TestCase
from django.urls import reverse
from django.utils import timezone, translation
from djmoney.money import Money

from company.active_company import ActiveCompany
from company.models import Company, CompanyUser
from inventory.models import InventoryItem
from invoices.models import AppliedTax, TaxType
from management.models import ProjectFile
from materials.models import PaymentMethod, Provider
from purchasing.admin import (
    ProjectPurchaseApprovalRuleAdmin, PurchaseOrderAdmin, PurchaseOrderDetailsSection,
)
from purchasing.models import (
    ProjectPurchaseApprovalRule,
    PurchaseOrder,
    PurchaseOrderItem,
    PurchaseOrderStatus,
    PurchaseOrderType,
)
from purchasing.tasks import process_due_purchase_order_approvals
from sales.models import Client


class ProjectPurchaseApprovalRuleTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name="Purchasing Co", tax_identifier="B12345678")
        cls.other_company = Company.objects.create(name="Other Co", tax_identifier="B87654321")
        cls.provider = Provider.objects.create(
            company=cls.company, name="Supplier", payment_terms_days=30
        )
        cls.other_provider = Provider.objects.create(company=cls.other_company, name="Other supplier")
        client = Client.objects.create(company=cls.company, name="Buyer")
        cls.project = ProjectFile.objects.create(company=cls.company, client=client, name="Project")
        other_client = Client.objects.create(company=cls.other_company, name="Other buyer")
        cls.other_project = ProjectFile.objects.create(
            company=cls.other_company, client=other_client, name="Other project"
        )
        cls.item = InventoryItem.objects.create(company=cls.company, code="ITEM-1", name="Material")
        cls.other_item = InventoryItem.objects.create(
            company=cls.other_company, code="ITEM-2", name="Other material"
        )
        cls.user = get_user_model().objects.create_superuser(
            username="purchasing_admin", password="pw", email="admin@example.com"
        )

    def setUp(self):
        self.po_admin = PurchaseOrderAdmin(PurchaseOrder, admin.site)

    def tearDown(self):
        ActiveCompany.clear_current()

    def _request(self):
        request = RequestFactory().post("/", {"_form_submitted": "on"})
        SessionMiddleware(lambda r: None).process_request(request)
        MessageMiddleware(lambda r: None).process_request(request)
        request.user = self.user
        return request

    def _order(self, amount="100.00", po_type=PurchaseOrderType.PROJECT, company=None):
        company = company or self.company
        po = PurchaseOrder.objects.create(
            company=company,
            provider=self.provider if company == self.company else self.other_provider,
            project_file=self.project if po_type == PurchaseOrderType.PROJECT and company == self.company else None,
            po_type=po_type,
            date_ordered=date(2026, 10, 1),
            requested_by=self.user,
        )
        if amount is not None:
            PurchaseOrderItem.objects.create(
                purchase_order=po,
                inventory_item=self.item,
                quantity=Decimal("1"),
                unit_price=Money(amount, "EUR"),
            )
        po.refresh_from_db()
        return po

    def _submit(self, po):
        with (
            patch("purchasing.tasks.send_purchase_order_pending_approval_notification.delay") as pending,
            patch("purchasing.tasks.send_purchase_order_decision_notification.delay") as decision,
            self.captureOnCommitCallbacks(execute=True),
        ):
            self.po_admin._send_to_approval(self._request(), po)
        po.refresh_from_db()
        return pending, decision

    def _set_project_revenue(self, amount):
        self.project.total_amount = Money(amount, "EUR")
        self.project.save(update_fields=["total_amount", "total_amount_currency"])

    def test_exact_limit_uses_net_amount_and_records_automatic_approval(self):
        ProjectPurchaseApprovalRule.objects.create(
            company=self.company, maximum_amount=Money("100", "EUR")
        )
        tax, _ = TaxType.objects.get_or_create(
            tax_name="IVA", country="ES",
            defaults={"behavior": "add", "percentage_options": [0, 21], "default_percentage": 21},
        )
        AppliedTax.objects.create(content_object=self.item, tax_type=tax, selected_percentage=21, order=0)
        po = self._order()
        self.assertGreater(po.total_amount_with_tax.amount, Decimal("100"))

        pending, decision = self._submit(po)

        self.assertEqual(po.status, PurchaseOrderStatus.APPROVED)
        self.assertTrue(po.approved_automatically)
        self.assertIsNotNone(po.approved_at)
        self.assertIsNone(po.approved_by)
        pending.assert_not_called()
        decision.assert_called_once_with(po.pk, "approved")
        self.project.refresh_from_db()
        self.assertEqual(self.project.total_purchased.amount, Decimal("100"))

    def test_delay_starts_when_submitted_and_approves_only_after_due_time(self):
        ProjectPurchaseApprovalRule.objects.create(
            company=self.company, maximum_amount=Money("500", "EUR"),
            automatic_approval_delay_minutes=10,
        )
        po = self._order()
        submitted_at = timezone.now()

        pending, decision = self._submit(po)

        self.assertEqual(po.status, PurchaseOrderStatus.PENDING_APPROVAL)
        self.assertFalse(po.approved_automatically)
        self.assertGreaterEqual(po.automatic_approval_due_at, submitted_at + timedelta(minutes=10))
        pending.assert_called_once_with(po.pk)
        decision.assert_not_called()
        self.assertEqual(process_due_purchase_order_approvals()["approved"], 0)

        PurchaseOrder.objects.filter(pk=po.pk).update(
            automatic_approval_due_at=timezone.now() - timedelta(seconds=1)
        )
        with (
            patch("purchasing.tasks.send_purchase_order_decision_notification.delay") as notified,
            self.captureOnCommitCallbacks(execute=True),
        ):
            self.assertEqual(process_due_purchase_order_approvals()["approved"], 1)

        po.refresh_from_db()
        self.assertEqual(po.status, PurchaseOrderStatus.APPROVED)
        self.assertTrue(po.approved_automatically)
        self.assertIsNone(po.automatic_approval_due_at)
        notified.assert_called_once_with(po.pk, "approved")
        self.project.refresh_from_db()
        self.assertEqual(self.project.total_purchased.amount, Decimal("100"))
        self.assertEqual(process_due_purchase_order_approvals()["approved"], 0)

    def test_manual_approval_during_delay_cancels_automatic_approval(self):
        ProjectPurchaseApprovalRule.objects.create(
            company=self.company, maximum_amount=Money("500", "EUR"),
            automatic_approval_delay_minutes=10,
        )
        po = self._order()
        self._submit(po)

        with (
            patch("purchasing.tasks.send_purchase_order_decision_notification.delay") as notified,
            self.captureOnCommitCallbacks(execute=True),
        ):
            self.po_admin._approve_po(self._request(), po)

        po.refresh_from_db()
        self.assertEqual(po.status, PurchaseOrderStatus.APPROVED)
        self.assertFalse(po.approved_automatically)
        self.assertEqual(po.approved_by, self.user)
        self.assertIsNone(po.automatic_approval_due_at)
        notified.assert_called_once_with(po.pk, "approved")
        self.assertEqual(process_due_purchase_order_approvals()["approved"], 0)

    def test_rejection_during_delay_cancels_automatic_approval(self):
        ProjectPurchaseApprovalRule.objects.create(
            company=self.company, maximum_amount=Money("500", "EUR"),
            automatic_approval_delay_minutes=10,
        )
        po = self._order()
        self._submit(po)
        self.client.force_login(self.user)
        cache.delete(ActiveCompany.SUPERUSER_IDS_CACHE_KEY)
        session = self.client.session
        session[ActiveCompany.SESSION_KEY] = [self.company.pk]
        session[ActiveCompany.SESSION_SET_AT_KEY] = time.time()
        session.save()
        with (
            patch("purchasing.tasks.send_purchase_order_decision_notification.delay") as notified,
            self.captureOnCommitCallbacks(execute=True),
        ):
            response = self.client.post(
                reverse("admin:purchasing_purchaseorder_reject", args=[po.pk]),
                {"reason": "Needs review"},
            )

        self.assertEqual(response.status_code, 302)
        po.refresh_from_db()
        self.assertEqual(po.status, PurchaseOrderStatus.REJECTED)
        self.assertIsNone(po.automatic_approval_due_at)
        notified.assert_called_once_with(po.pk, "rejected")
        self.assertEqual(process_due_purchase_order_approvals()["approved"], 0)

    def test_rule_is_checked_again_when_delay_expires(self):
        rule = ProjectPurchaseApprovalRule.objects.create(
            company=self.company, maximum_amount=Money("500", "EUR"),
            automatic_approval_delay_minutes=10,
        )
        po = self._order()
        self._submit(po)
        rule.is_active = False
        rule.save(update_fields=["is_active"])
        PurchaseOrder.objects.filter(pk=po.pk).update(
            automatic_approval_due_at=timezone.now() - timedelta(seconds=1)
        )

        with patch("purchasing.tasks.send_purchase_order_decision_notification.delay") as notified:
            self.assertEqual(process_due_purchase_order_approvals()["approved"], 0)

        po.refresh_from_db()
        self.assertEqual(po.status, PurchaseOrderStatus.PENDING_APPROVAL)
        self.assertIsNone(po.automatic_approval_due_at)
        notified.assert_not_called()

    def test_changed_payment_terms_require_manual_review_after_delay(self):
        ProjectPurchaseApprovalRule.objects.create(
            company=self.company, maximum_amount=Money("500", "EUR"),
            automatic_approval_delay_minutes=10,
        )
        po = self._order()
        self._submit(po)
        PurchaseOrder.objects.filter(pk=po.pk).update(
            payment_terms_days=0,
            automatic_approval_due_at=timezone.now() - timedelta(seconds=1),
        )

        self.assertEqual(process_due_purchase_order_approvals()["approved"], 0)
        po.refresh_from_db()
        self.assertEqual(po.status, PurchaseOrderStatus.PENDING_APPROVAL)
        self.assertIsNone(po.automatic_approval_due_at)

    def test_above_limit_stays_pending_for_manual_approval(self):
        ProjectPurchaseApprovalRule.objects.create(
            company=self.company, maximum_amount=Money("99.99", "EUR")
        )
        po = self._order()
        pending, decision = self._submit(po)

        self.assertEqual(po.status, PurchaseOrderStatus.PENDING_APPROVAL)
        self.assertFalse(po.approved_automatically)
        pending.assert_called_once_with(po.pk)
        decision.assert_not_called()

    def test_immediate_bank_transfer_requires_manual_approval(self):
        ProjectPurchaseApprovalRule.objects.create(
            company=self.company, maximum_amount=Money("500", "EUR")
        )
        po = self._order()
        po.payment_terms_days = 0
        po.save(update_fields=["payment_terms_days"])

        pending, decision = self._submit(po)

        self.assertEqual(po.status, PurchaseOrderStatus.PENDING_APPROVAL)
        self.assertFalse(po.approved_automatically)
        pending.assert_called_once_with(po.pk)
        decision.assert_not_called()

    def test_non_immediate_payment_still_approves_automatically(self):
        ProjectPurchaseApprovalRule.objects.create(
            company=self.company, maximum_amount=Money("500", "EUR")
        )
        transfer_later = self._order()
        immediate_card = self._order()
        immediate_card.payment_method = PaymentMethod.CARD
        immediate_card.payment_terms_days = 0
        immediate_card.save(update_fields=["payment_method", "payment_terms_days"])

        for po in (transfer_later, immediate_card):
            with self.subTest(payment_method=po.payment_method, days=po.payment_terms_days):
                pending, decision = self._submit(po)
                self.assertEqual(po.status, PurchaseOrderStatus.APPROVED)
                pending.assert_not_called()
                decision.assert_called_once_with(po.pk, "approved")

    def test_immediate_transfer_review_can_be_disabled_per_company(self):
        ProjectPurchaseApprovalRule.objects.create(
            company=self.company,
            maximum_amount=Money("500", "EUR"),
            require_manual_approval_for_immediate_transfer=False,
        )
        po = self._order()
        po.payment_terms_days = 0
        po.save(update_fields=["payment_terms_days"])

        pending, decision = self._submit(po)

        self.assertEqual(po.status, PurchaseOrderStatus.APPROVED)
        pending.assert_not_called()
        decision.assert_called_once_with(po.pk, "approved")

    def test_margin_at_minimum_approves_after_existing_committed_purchases(self):
        self._set_project_revenue("1000")
        existing = self._order(amount="200")
        existing.status = PurchaseOrderStatus.APPROVED
        existing.save(update_fields=["status"])
        self.project.refresh_from_db()
        self.assertEqual(self.project.total_purchased.amount, Decimal("200"))
        ProjectPurchaseApprovalRule.objects.create(
            company=self.company,
            maximum_amount=Money("500", "EUR"),
            minimum_margin_percent=Decimal("70.00"),
        )
        po = self._order(amount="100")
        self.assertEqual(po.projected_margin_percent, Decimal("70.00"))

        pending, decision = self._submit(po)

        self.assertEqual(po.status, PurchaseOrderStatus.APPROVED)
        pending.assert_not_called()
        decision.assert_called_once_with(po.pk, "approved")
        self.project.refresh_from_db()
        self.assertEqual(self.project.total_purchased.amount, Decimal("300"))

    def test_margin_below_minimum_requires_manual_approval(self):
        self._set_project_revenue("1000")
        ProjectPurchaseApprovalRule.objects.create(
            company=self.company,
            maximum_amount=Money("500", "EUR"),
            minimum_margin_percent=Decimal("70.00"),
        )
        po = self._order(amount="300.05")
        self.assertEqual(po.projected_margin_percent, Decimal("69.99500"))

        pending, decision = self._submit(po)

        self.assertEqual(po.status, PurchaseOrderStatus.PENDING_APPROVAL)
        pending.assert_called_once_with(po.pk)
        decision.assert_not_called()

    def test_margin_includes_other_project_costs(self):
        self._set_project_revenue("1000")
        self.project.total_costs = Money("100", "EUR")
        self.project.save(update_fields=["total_costs", "total_costs_currency"])
        ProjectPurchaseApprovalRule.objects.create(
            company=self.company,
            maximum_amount=Money("500", "EUR"),
            minimum_margin_percent=Decimal("70.00"),
        )
        po = self._order(amount="250")
        self.assertEqual(po.projected_margin_percent, Decimal("65.00"))
        self.assertIn("65.0%", self.po_admin.get_projected_margin(po))

        pending, decision = self._submit(po)

        self.assertEqual(po.status, PurchaseOrderStatus.PENDING_APPROVAL)
        pending.assert_called_once_with(po.pk)
        decision.assert_not_called()

    def test_margin_rule_requires_positive_project_revenue(self):
        ProjectPurchaseApprovalRule.objects.create(
            company=self.company,
            maximum_amount=Money("500", "EUR"),
            minimum_margin_percent=Decimal("0"),
        )
        po = self._order(amount="100")
        self.assertIsNone(po.projected_margin_percent)

        pending, decision = self._submit(po)

        self.assertEqual(po.status, PurchaseOrderStatus.PENDING_APPROVAL)
        pending.assert_called_once_with(po.pk)
        decision.assert_not_called()

    def test_margin_rule_rejects_percent_outside_zero_to_one_hundred(self):
        for value in (Decimal("-0.01"), Decimal("100.01")):
            with self.subTest(value=value):
                rule = ProjectPurchaseApprovalRule(
                    company=self.company,
                    maximum_amount=Money("500", "EUR"),
                    minimum_margin_percent=value,
                )
                with self.assertRaises(ValidationError) as caught:
                    rule.full_clean()
                self.assertIn("minimum_margin_percent", caught.exception.message_dict)

    def test_foreign_project_cannot_qualify_for_automatic_approval(self):
        ProjectPurchaseApprovalRule.objects.create(
            company=self.company,
            maximum_amount=Money("500", "EUR"),
            minimum_margin_percent=Decimal("0"),
        )
        po = self._order()
        po.project_file = self.other_project
        po.save(update_fields=["project_file"])

        pending, decision = self._submit(po)

        self.assertEqual(po.status, PurchaseOrderStatus.PENDING_APPROVAL)
        pending.assert_called_once_with(po.pk)
        decision.assert_not_called()

    def test_foreign_provider_or_item_cannot_qualify_for_automatic_approval(self):
        ProjectPurchaseApprovalRule.objects.create(
            company=self.company, maximum_amount=Money("500", "EUR")
        )
        foreign_provider = self._order()
        foreign_provider.provider = self.other_provider
        foreign_provider.save(update_fields=["provider"])

        foreign_item = self._order()
        foreign_item.items.update(inventory_item=self.other_item)

        for po in (foreign_provider, foreign_item):
            with self.subTest(po=po.pk):
                pending, decision = self._submit(po)
                self.assertEqual(po.status, PurchaseOrderStatus.PENDING_APPROVAL)
                pending.assert_called_once_with(po.pk)
                decision.assert_not_called()

    def test_automatic_approval_notifies_requester_without_pending_email(self):
        self.company.purchase_order_notification_emails = ["approver@example.com"]
        self.company.save(update_fields=["purchase_order_notification_emails"])
        ProjectPurchaseApprovalRule.objects.create(
            company=self.company, maximum_amount=Money("100", "EUR")
        )
        po = self._order()
        with self.captureOnCommitCallbacks(execute=True):
            self.po_admin._send_to_approval(self._request(), po)

        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["admin@example.com"])
        self.assertIn("Aprobada", mail.outbox[0].body)
        self.assertNotIn("aprobada automáticamente", mail.outbox[0].body.lower())
        self.assertNotIn("regla de aprobación", mail.outbox[0].body.lower())
        self.assertNotIn("purchasing_admin", mail.outbox[0].body)

    def test_employee_sees_no_automatic_approval_details(self):
        employee = get_user_model().objects.create_user(
            username="purchase_employee", password="pw", is_staff=True,
        )
        CompanyUser.objects.create(user=employee, company=self.company, is_active=True)
        po_content_type = ContentType.objects.get_for_model(PurchaseOrder)
        employee.user_permissions.add(*Permission.objects.filter(
            content_type=po_content_type,
            codename__in=("view_purchaseorder", "change_purchaseorder"),
        ))
        self.assertFalse(employee.has_perm("purchasing.view_projectpurchaseapprovalrule"))
        self.client.force_login(employee)
        session = self.client.session
        session[ActiveCompany.SESSION_KEY] = [self.company.pk]
        session[ActiveCompany.SESSION_SET_AT_KEY] = time.time()
        session.save()

        rule = ProjectPurchaseApprovalRule.objects.create(
            company=self.company, maximum_amount=Money("500", "EUR")
        )
        self.assertNotEqual(
            self.client.get(
                reverse("admin:purchasing_projectpurchaseapprovalrule_changelist")
            ).status_code,
            200,
        )
        po = self._order()
        submit_url = reverse("admin:purchasing_purchaseorder_send_to_approval", args=[po.pk])
        with translation.override("es"):
            dialog = self.client.get(submit_url)
        self.assertContains(dialog, "¿Enviar esta orden a aprobación?")
        self.assertNotContains(dialog, "aprobación automática")

        with (
            patch("purchasing.tasks.send_purchase_order_decision_notification.delay"),
            self.captureOnCommitCallbacks(execute=True),
        ):
            submitted = self.client.post(
                submit_url, {"_form_submitted": "on"}, follow=True,
            )
        self.assertEqual(submitted.status_code, 200)
        self.assertNotContains(submitted, "aprobada automáticamente")
        po.refresh_from_db()
        self.assertTrue(po.approved_automatically)

        with translation.override("es"):
            detail = self.client.get(
                reverse("admin:purchasing_purchaseorder_change", args=[po.pk])
            )
        self.assertEqual(detail.status_code, 200)
        self.assertNotContains(detail, "Aprobada automáticamente")
        self.assertNotContains(detail, "Aprobación automática prevista para")
        section_request = RequestFactory().get("/")
        section_request.user = employee
        with translation.override("es"):
            section = PurchaseOrderDetailsSection(section_request, po).render()
        self.assertNotIn("Aprobación automática", section)
        self.assertNotIn("Aprobado por", section)

        rule.automatic_approval_delay_minutes = 10
        rule.save(update_fields=["automatic_approval_delay_minutes"])
        waiting_po = self._order()
        self._submit(waiting_po)
        self.assertIsNotNone(waiting_po.automatic_approval_due_at)
        with translation.override("es"):
            waiting_detail = self.client.get(
                reverse("admin:purchasing_purchaseorder_change", args=[waiting_po.pk])
            )
        self.assertEqual(waiting_detail.status_code, 200)
        self.assertNotContains(waiting_detail, "Aprobación automática prevista para")

        manager_request = RequestFactory().get("/")
        manager_request.user = self.user
        with translation.override("es"):
            manager_section = PurchaseOrderDetailsSection(manager_request, po).render()
        self.assertIn("Aprobación automática", manager_section)
        self.assertIn(
            "approved_automatically",
            str(self.po_admin.get_fieldsets(manager_request, po)),
        )

    def test_missing_inactive_or_other_company_rule_does_not_approve(self):
        ProjectPurchaseApprovalRule.objects.create(
            company=self.other_company, maximum_amount=Money("1000", "EUR")
        )
        first = self._order()
        self._submit(first)
        self.assertEqual(first.status, PurchaseOrderStatus.PENDING_APPROVAL)

        ProjectPurchaseApprovalRule.objects.create(
            company=self.company, maximum_amount=Money("1000", "EUR"), is_active=False
        )
        second = self._order()
        self._submit(second)
        self.assertEqual(second.status, PurchaseOrderStatus.PENDING_APPROVAL)

    def test_only_project_orders_with_lines_are_eligible(self):
        ProjectPurchaseApprovalRule.objects.create(
            company=self.company, maximum_amount=Money("1000", "EUR")
        )
        stock = self._order(po_type=PurchaseOrderType.STOCK)
        empty = self._order(amount=None)
        self._submit(stock)
        self._submit(empty)
        self.assertEqual(stock.status, PurchaseOrderStatus.PENDING_APPROVAL)
        self.assertEqual(empty.status, PurchaseOrderStatus.PENDING_APPROVAL)

    def test_repeating_submission_does_not_send_a_second_notification(self):
        ProjectPurchaseApprovalRule.objects.create(
            company=self.company, maximum_amount=Money("1000", "EUR")
        )
        po = self._order()
        self._submit(po)
        pending, decision = self._submit(po)
        self.assertEqual(po.status, PurchaseOrderStatus.APPROVED)
        pending.assert_not_called()
        decision.assert_not_called()

    def test_admin_action_requires_post_before_automatic_approval(self):
        ProjectPurchaseApprovalRule.objects.create(
            company=self.company, maximum_amount=Money("500", "EUR")
        )
        po = self._order()
        self.client.force_login(self.user)
        cache.delete(ActiveCompany.SUPERUSER_IDS_CACHE_KEY)
        session = self.client.session
        session[ActiveCompany.SESSION_KEY] = [self.company.pk]
        session[ActiveCompany.SESSION_SET_AT_KEY] = time.time()
        session.save()
        url = reverse("admin:purchasing_purchaseorder_send_to_approval", args=[po.pk])

        response = self.client.get(url)
        po.refresh_from_db()
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "¿Enviar esta orden a aprobación?")
        self.assertNotContains(response, "automáticamente")
        self.assertEqual(po.status, PurchaseOrderStatus.DRAFT)

        with (
            patch("purchasing.tasks.send_purchase_order_decision_notification.delay") as decision,
            self.captureOnCommitCallbacks(execute=True),
        ):
            response = self.client.post(url, {"_form_submitted": "on"}, HTTP_HX_REQUEST="true")

        po.refresh_from_db()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response["HX-Redirect"], reverse("admin:purchasing_purchaseorder_changelist")
        )
        self.assertEqual(po.status, PurchaseOrderStatus.APPROVED)
        decision.assert_called_once_with(po.pk, "approved")

        detail_po = self._order()
        detail_url = reverse(
            "admin:purchasing_purchaseorder_send_to_approval_detail_action",
            args=[detail_po.pk],
        )
        self.assertEqual(self.client.get(detail_url).status_code, 200)
        detail_po.refresh_from_db()
        self.assertEqual(detail_po.status, PurchaseOrderStatus.DRAFT)
        with (
            patch("purchasing.tasks.send_purchase_order_decision_notification.delay") as detail_decision,
            self.captureOnCommitCallbacks(execute=True),
        ):
            detail_response = self.client.post(
                detail_url, {"_form_submitted": "on"}, HTTP_HX_REQUEST="true"
            )
        detail_po.refresh_from_db()
        self.assertEqual(detail_response.status_code, 200)
        self.assertEqual(
            detail_response["HX-Redirect"],
            reverse("admin:purchasing_purchaseorder_change", args=[detail_po.pk]),
        )
        self.assertEqual(detail_po.status, PurchaseOrderStatus.APPROVED)
        detail_decision.assert_called_once_with(detail_po.pk, "approved")

    def test_view_only_user_cannot_submit_for_automatic_approval(self):
        ProjectPurchaseApprovalRule.objects.create(
            company=self.company, maximum_amount=Money("500", "EUR")
        )
        po = self._order()
        user = get_user_model().objects.create_user(
            username="po_viewer", password="pw", is_staff=True
        )
        permission = Permission.objects.get(
            content_type=ContentType.objects.get_for_model(PurchaseOrder),
            codename="view_purchaseorder",
        )
        user.user_permissions.add(permission)
        self.client.force_login(user)
        url = reverse("admin:purchasing_purchaseorder_send_to_approval", args=[po.pk])

        response = self.client.post(url)

        self.assertEqual(response.status_code, 403)
        po.refresh_from_db()
        self.assertEqual(po.status, PurchaseOrderStatus.DRAFT)

    def test_rule_requires_positive_maximum(self):
        rule = ProjectPurchaseApprovalRule(
            company=self.company, maximum_amount=Money("0", "EUR")
        )
        with self.assertRaises(ValidationError) as caught:
            rule.full_clean()
        self.assertIn("maximum_amount", caught.exception.message_dict)

    def test_rule_admin_and_submission_follow_active_company_scope(self):
        ProjectPurchaseApprovalRule.objects.create(
            company=self.other_company, maximum_amount=Money("1000", "EUR")
        )
        ActiveCompany.set_current_ids([self.company.pk])
        request = self._request()
        rule_admin = ProjectPurchaseApprovalRuleAdmin(ProjectPurchaseApprovalRule, admin.site)
        self.assertFalse(rule_admin.get_queryset(request).exists())
        self.assertEqual(
            list(rule_admin.formfield_for_foreignkey(
                ProjectPurchaseApprovalRule._meta.get_field("company"), request
            ).queryset),
            [self.company],
        )

        foreign_po = self._order(amount=None, po_type=PurchaseOrderType.STOCK, company=self.other_company)
        with self.assertRaises(Http404):
            self.po_admin.send_to_approval(request, foreign_po.pk)
        foreign_po.refresh_from_db()
        self.assertEqual(foreign_po.status, PurchaseOrderStatus.DRAFT)

    def test_rejection_view_cannot_access_another_company(self):
        foreign_po = self._order(
            amount=None, po_type=PurchaseOrderType.STOCK, company=self.other_company
        )
        foreign_po.status = PurchaseOrderStatus.PENDING_APPROVAL
        foreign_po.save(update_fields=["status"])
        self.client.force_login(self.user)
        cache.delete(ActiveCompany.SUPERUSER_IDS_CACHE_KEY)
        session = self.client.session
        session[ActiveCompany.SESSION_KEY] = [self.company.pk]
        session[ActiveCompany.SESSION_SET_AT_KEY] = time.time()
        session.save()
        url = reverse("admin:purchasing_purchaseorder_reject", args=[foreign_po.pk])

        self.assertEqual(self.client.get(url).status_code, 404)
        self.assertEqual(self.client.post(url, {"reason": "No"}).status_code, 404)
        foreign_po.refresh_from_db()
        self.assertEqual(foreign_po.status, PurchaseOrderStatus.PENDING_APPROVAL)

    def test_rule_admin_form_shows_configurable_conditions_in_spanish(self):
        self.client.force_login(self.user)
        with translation.override("es"):
            response = self.client.get(reverse("admin:purchasing_projectpurchaseapprovalrule_add"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'name="minimum_margin_percent"')
        self.assertContains(response, "Margen mínimo del proyecto (%)")
        self.assertContains(response, 'name="require_manual_approval_for_immediate_transfer"')
        self.assertContains(response, "Revisar transferencias inmediatas")
        self.assertContains(response, 'name="automatic_approval_delay_minutes"')
        self.assertContains(response, "Espera para la aprobación automática (minutos)")
