"""
purchasing/tests/test_po_pending_approval_email.py

The company's configured approvers are emailed when a PO is sent to approval —
not when it is created.

Run:
    python manage.py test purchasing.tests.test_po_pending_approval_email
"""
from datetime import date

from django.contrib import admin
from django.contrib.auth import get_user_model
from django.contrib.messages.middleware import MessageMiddleware
from django.contrib.sessions.middleware import SessionMiddleware
from django.core import mail
from django.test import RequestFactory, TestCase

from company.models import Company
from materials.models import Provider
from purchasing.admin import PurchaseOrderAdmin
from purchasing.models import PurchaseOrder, PurchaseOrderStatus
from purchasing.tasks import send_purchase_order_pending_approval_notification

User = get_user_model()


class PendingApprovalEmailTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(
            name="Test Company",
            tax_identifier="B12345678",
            purchase_order_notification_emails=["approver@example.com"],
        )
        self.provider = Provider.objects.create(company=self.company, name="Proveedor SA")
        self.user = User.objects.create_superuser(
            username="requester", password="x", email="requester@example.com",
            first_name="Rita", last_name="Requester",
        )

    def _make_po(self, status=PurchaseOrderStatus.DRAFT):
        return PurchaseOrder.objects.create(
            company=self.company,
            provider=self.provider,
            date_ordered=date(2026, 8, 1),
            status=status,
            requested_by=self.user,
        )

    def _admin_request(self):
        req = RequestFactory().post("/")
        SessionMiddleware(lambda r: None).process_request(req)
        req.session.save()
        MessageMiddleware(lambda r: None).process_request(req)
        req.user = self.user
        return req

    def test_creating_po_sends_no_email(self):
        with self.captureOnCommitCallbacks(execute=True):
            self._make_po()
        self.assertEqual(len(mail.outbox), 0)

    def test_send_to_approval_emails_approvers(self):
        po = self._make_po()
        po_admin = PurchaseOrderAdmin(PurchaseOrder, admin.site)
        with self.captureOnCommitCallbacks(execute=True):
            po_admin._send_to_approval(self._admin_request(), po)

        po.refresh_from_db()
        self.assertEqual(po.status, PurchaseOrderStatus.PENDING_APPROVAL)
        self.assertEqual(len(mail.outbox), 1)
        message = mail.outbox[0]
        self.assertEqual(message.to, ["approver@example.com"])
        self.assertIn("pendiente de aprobación", message.subject)
        self.assertIn(po.number, message.subject)
        self.assertIn("Rita Requester", message.body)
        self.assertIn("Transferencia inmediata (pago a 0 días)", message.body)

    def test_send_to_approval_without_recipients_sends_nothing(self):
        self.company.purchase_order_notification_emails = []
        self.company.save()
        po = self._make_po()
        po_admin = PurchaseOrderAdmin(PurchaseOrder, admin.site)
        with self.captureOnCommitCallbacks(execute=True):
            po_admin._send_to_approval(self._admin_request(), po)
        po.refresh_from_db()
        self.assertEqual(po.status, PurchaseOrderStatus.PENDING_APPROVAL)
        self.assertEqual(len(mail.outbox), 0)

    def test_send_to_approval_on_non_draft_sends_nothing(self):
        po = self._make_po(status=PurchaseOrderStatus.APPROVED)
        po_admin = PurchaseOrderAdmin(PurchaseOrder, admin.site)
        with self.captureOnCommitCallbacks(execute=True):
            po_admin._send_to_approval(self._admin_request(), po)
        po.refresh_from_db()
        self.assertEqual(po.status, PurchaseOrderStatus.APPROVED)
        self.assertEqual(len(mail.outbox), 0)

    def test_delayed_pending_email_skips_order_already_approved(self):
        po = self._make_po(status=PurchaseOrderStatus.APPROVED)

        result = send_purchase_order_pending_approval_notification(po.pk)

        self.assertEqual(result, {"status": "skipped", "reason": "not_pending"})
        self.assertEqual(len(mail.outbox), 0)
