"""
purchasing/tests/test_po_decision_emails.py

Approving or rejecting a Purchase Order emails the decision to the user who
requested it (PurchaseOrder.requested_by).

Run:
    python manage.py test purchasing.tests.test_po_decision_emails
"""
from datetime import date

from django.contrib import admin
from django.contrib.auth import get_user_model
from django.contrib.messages.middleware import MessageMiddleware
from django.contrib.sessions.middleware import SessionMiddleware
from django.core import mail
from django.test import RequestFactory, TestCase
from django.urls import reverse

from company.models import Company
from materials.models import Provider
from purchasing.admin import PurchaseOrderAdmin
from purchasing.models import PurchaseOrder, PurchaseOrderStatus

User = get_user_model()


class PODecisionEmailTestBase(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Test Company", tax_identifier="B12345678")
        self.provider = Provider.objects.create(company=self.company, name="Proveedor SA")
        self.requester = User.objects.create_user(
            username="requester", password="x", email="requester@example.com",
            first_name="Rita", last_name="Requester",
        )
        self.reviewer = User.objects.create_superuser(username="reviewer", password="x", email="rev@example.com")
        self.po = PurchaseOrder.objects.create(
            company=self.company,
            provider=self.provider,
            date_ordered=date(2026, 8, 1),
            status=PurchaseOrderStatus.PENDING_APPROVAL,
            requested_by=self.requester,
        )

    def _admin_request(self):
        req = RequestFactory().post("/")
        SessionMiddleware(lambda r: None).process_request(req)
        req.session.save()
        MessageMiddleware(lambda r: None).process_request(req)
        req.user = self.reviewer
        return req


class ApprovePOEmailTests(PODecisionEmailTestBase):
    def _approve(self, po):
        po_admin = PurchaseOrderAdmin(PurchaseOrder, admin.site)
        with self.captureOnCommitCallbacks(execute=True):
            po_admin._approve_po(self._admin_request(), po)

    def test_approve_sets_fields_and_emails_requester(self):
        self._approve(self.po)
        self.po.refresh_from_db()
        self.assertEqual(self.po.status, PurchaseOrderStatus.APPROVED)
        self.assertEqual(self.po.approved_by, self.reviewer)
        self.assertIsNotNone(self.po.approved_at)

        self.assertEqual(len(mail.outbox), 1)
        message = mail.outbox[0]
        self.assertEqual(message.to, ["requester@example.com"])
        self.assertIn(self.po.number, message.subject)
        self.assertIn("Aprobada", message.subject)
        self.assertIn("Aprobada", message.body)
        self.assertIn(reverse("admin:purchasing_purchaseorder_change", args=[self.po.pk]), message.body)

    def test_approve_without_requester_email_sends_nothing(self):
        self.requester.email = ""
        self.requester.save()
        self._approve(self.po)
        self.po.refresh_from_db()
        self.assertEqual(self.po.status, PurchaseOrderStatus.APPROVED)
        self.assertEqual(len(mail.outbox), 0)

    def test_approve_non_pending_po_does_nothing(self):
        self.po.status = PurchaseOrderStatus.DRAFT
        self.po.save()
        self._approve(self.po)
        self.po.refresh_from_db()
        self.assertEqual(self.po.status, PurchaseOrderStatus.DRAFT)
        self.assertEqual(len(mail.outbox), 0)


class RejectPOEmailTests(PODecisionEmailTestBase):
    def _reject_url(self, po):
        return reverse("admin:purchasing_purchaseorder_reject", args=[po.pk])

    def test_reject_view_sets_fields_and_emails_requester(self):
        self.client.force_login(self.reviewer)
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(self._reject_url(self.po), {"reason": "Presupuesto excedido"})
        self.assertEqual(response.status_code, 302)

        self.po.refresh_from_db()
        self.assertEqual(self.po.status, PurchaseOrderStatus.REJECTED)
        self.assertEqual(self.po.rejected_by, self.reviewer)
        self.assertIsNotNone(self.po.rejected_at)
        self.assertEqual(self.po.rejection_reason, "Presupuesto excedido")

        self.assertEqual(len(mail.outbox), 1)
        message = mail.outbox[0]
        self.assertEqual(message.to, ["requester@example.com"])
        self.assertIn("Rechazada", message.subject)
        self.assertIn("Presupuesto excedido", message.body)

    def test_reject_requires_reason(self):
        self.client.force_login(self.reviewer)
        response = self.client.post(self._reject_url(self.po), {"reason": ""})
        self.assertEqual(response.status_code, 200)
        self.po.refresh_from_db()
        self.assertEqual(self.po.status, PurchaseOrderStatus.PENDING_APPROVAL)
        self.assertEqual(len(mail.outbox), 0)

    def test_reject_non_pending_po_is_refused(self):
        self.po.status = PurchaseOrderStatus.APPROVED
        self.po.save()
        self.client.force_login(self.reviewer)
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(self._reject_url(self.po), {"reason": "Tarde"})
        self.po.refresh_from_db()
        self.assertEqual(self.po.status, PurchaseOrderStatus.APPROVED)
        self.assertEqual(len(mail.outbox), 0)

    def test_reject_requires_approve_permission(self):
        plain_user = User.objects.create_user(username="plain", password="x", is_staff=True)
        self.client.force_login(plain_user)
        response = self.client.post(self._reject_url(self.po), {"reason": "No"})
        self.assertIn(response.status_code, (302, 403))
        self.po.refresh_from_db()
        self.assertEqual(self.po.status, PurchaseOrderStatus.PENDING_APPROVAL)
        self.assertEqual(len(mail.outbox), 0)
