from unittest.mock import patch

from django.contrib.auth.models import User
from django.core import mail
from django.test import TestCase
from django.urls import reverse

from company.models import Company, EmailLog, EmailReplyTo
from core.services.pdf_service import PDFService
from sales.models import (
    Client, ClientContact, DeliveryOrder, DeliveryOrderStatus, ShippingAgency,
)
from sales.services.delivery_order_email_service import DeliveryOrderEmailService


class DeliveryOrderEmailTestBase(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Test Co")
        self.client_obj = Client.objects.create(
            company=self.company,
            name="Cliente SA",
            email="client@example.com",
        )
        self.contact = ClientContact.objects.create(
            client=self.client_obj,
            name="Ana",
            email="ana@example.com",
            position="Compras",
        )
        self.agency = ShippingAgency.objects.create(
            company=self.company,
            name="SEUR",
            tracking_url_template="https://seur.com/track?ref={shipment_number}",
        )
        self.delivery_order = DeliveryOrder.objects.create(
            company=self.company,
            client=self.client_obj,
            shipping_agency=self.agency,
            shipment_number="EXP-123",
            package_count=3,
        )


class DeliveryOrderSendEmailViewTest(DeliveryOrderEmailTestBase):
    def setUp(self):
        super().setUp()
        self.user = User.objects.create_superuser(
            username="admin", email="admin@example.com", password="secret"
        )
        self.client.force_login(self.user)
        self.url = reverse(
            "admin:sales_deliveryorder_send_email", args=[self.delivery_order.pk]
        )

    def test_get_shows_client_and_contact_recipients(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "client@example.com")
        self.assertContains(response, "ana@example.com")

    def test_post_queues_email_with_sender_cc_and_reply_to(self):
        with patch(
            "sales.views.delivery_order_send_email_view.send_delivery_order_email_task"
        ) as task:
            response = self.client.post(
                self.url,
                {"to_emails": ["client@example.com", "ana@example.com"]},
            )
        task.delay.assert_called_once_with(
            self.delivery_order.id,
            to_emails=["client@example.com", "ana@example.com"],
            cc_emails=["admin@example.com"],
            reply_to=["admin@example.com"],
        )
        self.assertRedirects(
            response,
            reverse("admin:sales_deliveryorder_change", args=[self.delivery_order.pk]),
        )

    @patch.object(PDFService, "render_object_pdf", return_value=b"%PDF-fake")
    def test_post_end_to_end_replies_to_the_sending_user(self, mock_pdf):
        """
        The whole chain with Celery eager: admin view → task → service →
        message, so a broken hand-off anywhere shows up as a wrong header.
        """
        self.company.reply_to_email = "hola@acme.test"
        self.company.save(update_fields=["reply_to_email"])
        EmailReplyTo.objects.create(
            company=self.company,
            email_document="delivery_order",
            reply_to_email="albaranes@acme.test",
        )

        self.client.post(self.url, {"to_emails": ["client@example.com"]})

        self.assertEqual(len(mail.outbox), 1)
        # The sender beats both the per-document and the generic address.
        self.assertEqual(mail.outbox[0].reply_to, ["admin@example.com"])

    @patch.object(PDFService, "render_object_pdf", return_value=b"%PDF-fake")
    def test_post_end_to_end_falls_back_to_document_config(self, mock_pdf):
        self.user.email = ""
        self.user.save(update_fields=["email"])
        EmailReplyTo.objects.create(
            company=self.company,
            email_document="delivery_order",
            reply_to_email="albaranes@acme.test",
        )

        self.client.post(self.url, {"to_emails": ["client@example.com"]})

        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].reply_to, ["albaranes@acme.test"])

    def test_post_without_recipients_shows_error(self):
        with patch(
            "sales.views.delivery_order_send_email_view.send_delivery_order_email_task"
        ) as task:
            response = self.client.post(self.url, {"to_emails": []})
        task.delay.assert_not_called()
        self.assertEqual(response.status_code, 200)


class DeliveryOrderEmailServiceTest(DeliveryOrderEmailTestBase):
    @patch.object(PDFService, "render_object_pdf", return_value=b"%PDF-fake")
    def test_send_attaches_pdf_logs_and_marks_sent(self, mock_pdf):
        ok = DeliveryOrderEmailService.send_delivery_order_email(
            self.delivery_order,
            to_emails=["client@example.com"],
            cc_emails=["admin@example.com"],
        )
        self.assertTrue(ok)

        self.assertEqual(len(mail.outbox), 1)
        message = mail.outbox[0]
        self.assertEqual(message.to, ["client@example.com"])
        self.assertEqual(message.cc, ["admin@example.com"])
        self.assertIn(self.delivery_order.number, message.subject)
        filename, content, mimetype = message.attachments[0]
        self.assertEqual(filename, f"Albaran_{self.delivery_order.number}.pdf")
        self.assertEqual(mimetype, "application/pdf")

        self.delivery_order.refresh_from_db()
        self.assertEqual(self.delivery_order.status, DeliveryOrderStatus.SENT)

        log = EmailLog.objects.get(object_id=self.delivery_order.pk)
        self.assertEqual(log.status, "success")
        self.assertEqual(log.company, self.company)

    @patch.object(PDFService, "render_object_pdf", return_value=None)
    def test_send_without_template_fails_and_stays_draft(self, mock_pdf):
        ok = DeliveryOrderEmailService.send_delivery_order_email(
            self.delivery_order, to_emails=["client@example.com"]
        )
        self.assertFalse(ok)
        self.assertEqual(len(mail.outbox), 0)
        self.delivery_order.refresh_from_db()
        self.assertEqual(self.delivery_order.status, DeliveryOrderStatus.DRAFT)

    def test_tracking_link_built_from_agency_template(self):
        self.assertEqual(
            self.delivery_order.tracking_link,
            "https://seur.com/track?ref=EXP-123",
        )
