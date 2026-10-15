"""
purchasing/tests/test_po_messages.py

The Conversation tab of a Purchase Order: who may write in it, who gets the
notification email, and the unread alert on the changelist.

Run:
    python manage.py test purchasing.tests.test_po_messages
"""
import re
from datetime import date

from django.contrib import admin
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core import mail
from django.core.cache import cache
from django.test import RequestFactory, TestCase
from django.urls import reverse

from company.active_company import ActiveCompany
from company.models import CompanyUser
from purchasing.admin import PurchaseOrderMessageInline
from purchasing.models import (
    PurchaseOrder,
    PurchaseOrderMessage,
    PurchaseOrderStatus,
)
from purchasing.tests.factories import make_company, make_provider, make_purchase_order

User = get_user_model()


class POMessageTestBase(TestCase):
    def setUp(self):
        cache.clear()  # LocMem cache persists across tests in-process
        self.addCleanup(ActiveCompany.clear_current)

        self.company = make_company(name="Alpha")
        self.other_company = make_company(name="Beta", tax_identifier="B87654321")
        self.provider = make_provider(company=self.company)

        self.requester = self._make_user("requester", self.company)
        self.approver = self._make_user("approver", self.company, can_approve=True)
        self.second_approver = self._make_user("approver2", self.company, can_approve=True)
        self.bystander = self._make_user("bystander", self.company)
        self.outsider = self._make_user("outsider", self.other_company)

        self.po = make_purchase_order(
            company=self.company,
            provider=self.provider,
            date_ordered=date(2026, 8, 1),
            status=PurchaseOrderStatus.PENDING_APPROVAL,
            requested_by=self.requester,
        )

    def _make_user(self, username, company, can_approve=False):
        user = User.objects.create_user(
            username=username,
            password="x",
            email=f"{username}@example.com",
            is_staff=True,
        )
        CompanyUser.objects.create(user=user, company=company, is_active=True)
        codenames = ["view_purchaseorder"]
        if can_approve:
            codenames.append("can_approve_purchase_order")
        user.user_permissions.add(
            *Permission.objects.filter(
                codename__in=codenames, content_type__app_label="purchasing"
            )
        )
        return user

    def _post_url(self, po=None):
        return reverse(
            "admin:purchasing_purchaseorder_post_message", args=[(po or self.po).pk]
        )

    def _change_url(self, po=None):
        return reverse(
            "admin:purchasing_purchaseorder_change", args=[(po or self.po).pk]
        )

    def _post_message(self, user, body="¿Para qué necesitáis esto?", po=None):
        self.client.force_login(user)
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.post(self._post_url(po), {"body": body})

    def _write(self, sender, body="texto", recipient=None):
        """Create a message without going through the view."""
        return PurchaseOrderMessage.objects.create(
            purchase_order=self.po, sender=sender, body=body, recipient=recipient
        )


class POMessageRecipientTests(POMessageTestBase):
    def test_approver_message_goes_to_the_requester(self):
        response = self._post_message(self.approver)
        self.assertEqual(response.status_code, 302)

        message = PurchaseOrderMessage.objects.get()
        self.assertEqual(message.sender, self.approver)
        self.assertEqual(message.recipient, self.requester)
        self.assertIsNone(message.read_at)

        self.assertEqual(len(mail.outbox), 1)
        email = mail.outbox[0]
        self.assertEqual(email.to, ["requester@example.com"])
        self.assertEqual(email.reply_to, ["approver@example.com"])
        self.assertIn(self.po.number, email.subject)
        self.assertIn("¿Para qué necesitáis esto?", email.body)

    def test_requester_reply_goes_back_to_whoever_asked(self):
        self._write(self.approver)
        mail.outbox = []

        self._post_message(self.requester, body="Para la obra de Vigo")

        reply = PurchaseOrderMessage.objects.latest("pk")
        self.assertEqual(reply.recipient, self.approver)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["approver@example.com"])

    def test_requester_reply_goes_to_the_latest_asker(self):
        self._write(self.approver)
        self._write(self.second_approver)
        mail.outbox = []

        self._post_message(self.requester, body="Respuesta")

        self.assertEqual(PurchaseOrderMessage.objects.latest("pk").recipient, self.second_approver)
        self.assertEqual(mail.outbox[0].to, ["approver2@example.com"])

    def test_requester_opening_the_thread_falls_back_to_the_company_list(self):
        self.company.purchase_order_notification_emails = ["compras@example.com"]
        self.company.save(update_fields=["purchase_order_notification_emails"])

        self._post_message(self.requester, body="Necesito esto para el taller")

        message = PurchaseOrderMessage.objects.get()
        self.assertIsNone(message.recipient)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["compras@example.com"])

    def test_no_recipient_and_no_company_list_sends_nothing(self):
        self._post_message(self.requester, body="Necesito esto")

        self.assertEqual(PurchaseOrderMessage.objects.count(), 1)
        self.assertEqual(len(mail.outbox), 0)

    def test_recipient_without_email_sends_nothing(self):
        self.requester.email = ""
        self.requester.save(update_fields=["email"])

        self._post_message(self.approver)

        self.assertEqual(PurchaseOrderMessage.objects.count(), 1)
        self.assertEqual(len(mail.outbox), 0)

    def test_empty_message_is_rejected(self):
        response = self._post_message(self.approver, body="   ")

        self.assertEqual(response.status_code, 302)
        self.assertEqual(PurchaseOrderMessage.objects.count(), 0)
        self.assertEqual(len(mail.outbox), 0)


class POMessageEmailThreadTests(POMessageTestBase):
    """The notification repeats the conversation so far, capped."""

    def test_first_message_has_no_history_section(self):
        self._post_message(self.approver, body="primera pregunta")

        self.assertNotIn("Mensajes anteriores", mail.outbox[0].body)

    def test_email_carries_the_earlier_messages_newest_first(self):
        self._write(self.approver, body="pregunta uno")
        self._write(self.requester, body="respuesta uno")
        mail.outbox = []

        self._post_message(self.approver, body="pregunta dos")

        body = mail.outbox[0].body
        self.assertIn("Mensajes anteriores", body)
        self.assertLess(body.index("pregunta dos"), body.index("respuesta uno"))
        self.assertLess(body.index("respuesta uno"), body.index("pregunta uno"))
        self.assertNotIn("anteriores más", body)  # nothing was left out

    def test_history_is_capped_and_reports_the_remainder(self):
        limit = PurchaseOrderMessage.EMAIL_HISTORY_LIMIT
        for n in range(1, limit + 4):  # three more than fit
            self._write(self.approver, body=f"mensaje {n}")
        mail.outbox = []

        self._post_message(self.requester, body="respuesta")

        body = mail.outbox[0].body
        total = limit + 3
        for kept in range(total, total - limit, -1):  # the newest five
            self.assertIn(f"mensaje {kept}", body)
        for dropped in range(1, total - limit + 1):  # the oldest three
            self.assertNotIn(f"mensaje {dropped}", body)
        self.assertIn("Y 3 mensajes anteriores más", body)

    def test_long_earlier_message_is_excerpted(self):
        chars = PurchaseOrderMessage.EMAIL_HISTORY_EXCERPT_CHARS
        self._write(self.approver, body="x" * (chars + 200))
        mail.outbox = []

        self._post_message(self.requester, body="respuesta")

        body = mail.outbox[0].body
        self.assertIn("…", body)
        self.assertNotIn("x" * (chars + 1), body)

    def test_new_message_itself_is_never_truncated(self):
        chars = PurchaseOrderMessage.EMAIL_HISTORY_EXCERPT_CHARS
        long_body = "y" * (chars + 200)

        self._post_message(self.approver, body=long_body)

        self.assertIn(long_body, mail.outbox[0].body)


class POMessageAccessTests(POMessageTestBase):
    def test_requester_can_write_on_an_approved_order(self):
        self.po.status = PurchaseOrderStatus.APPROVED
        self.po.save(update_fields=["status"])

        response = self._post_message(self.requester, body="¿Cuándo llega?")

        self.assertEqual(response.status_code, 302)
        self.assertEqual(PurchaseOrderMessage.objects.count(), 1)
        self.po.refresh_from_db()
        self.assertEqual(self.po.status, PurchaseOrderStatus.APPROVED)

    def test_bystander_cannot_write(self):
        response = self._post_message(self.bystander, body="Yo tambien opino")

        self.assertEqual(response.status_code, 403)
        self.assertEqual(PurchaseOrderMessage.objects.count(), 0)

    def test_bystander_without_the_permission_sees_no_conversation(self):
        self._write(self.approver, body="Pregunta del revisor")
        self.client.force_login(self.bystander)

        response = self.client.get(self._change_url())

        # Not merely read-only: no tab, no thread, no hint one exists.
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "Pregunta del revisor")
        self.assertNotContains(response, 'href="#messages"')
        self.assertNotContains(response, 'id="messages-group"')
        self.assertNotContains(response, 'id="po-message-form"')

    def test_view_permission_grants_read_access_without_writing(self):
        self._write(self.approver, body="Pregunta del revisor")
        self.bystander.user_permissions.add(
            Permission.objects.get(
                codename="view_purchaseordermessage",
                content_type__app_label="purchasing",
            )
        )
        self.client.force_login(self.bystander)

        response = self.client.get(self._change_url())

        self.assertContains(response, "Pregunta del revisor")
        self.assertContains(response, 'href="#messages"')
        self.assertNotContains(response, 'id="po-message-form"')  # read only

        posted = self._post_message(self.bystander, body="no deberia entrar")
        self.assertEqual(posted.status_code, 403)

    def test_superuser_can_read_and_write(self):
        admin_user = User.objects.create_superuser(
            username="root", password="x", email="root@example.com"
        )
        CompanyUser.objects.create(user=admin_user, company=self.company, is_active=True)
        self._write(self.approver, body="Pregunta del revisor")
        self.client.force_login(admin_user)

        response = self.client.get(self._change_url())

        self.assertContains(response, "Pregunta del revisor")
        self.assertContains(response, 'id="po-message-form"')

    def test_approver_who_never_wrote_can_still_read_the_thread(self):
        # Approvers are a group, not one account: whoever covers for the
        # colleague who asked must be able to pick the conversation up.
        self._write(self.approver, body="Pregunta del revisor")
        self.client.force_login(self.second_approver)

        response = self.client.get(self._change_url())

        self.assertContains(response, "Pregunta del revisor")
        self.assertContains(response, 'id="po-message-form"')

    def test_other_company_cannot_write(self):
        response = self._post_message(self.outsider, body="Hola")

        self.assertEqual(response.status_code, 404)
        self.assertEqual(PurchaseOrderMessage.objects.count(), 0)

    def test_own_bubble_hugs_its_text_and_keeps_line_breaks(self):
        self._write(self.requester, body="primera linea\nsegunda linea")
        self.client.force_login(self.requester)

        html = self.client.get(self._change_url()).content.decode()

        # Unfold's compiled CSS has no items-end or whitespace-pre-line, so the
        # bubble is sized with w-fit/ml-auto and the breaks come from the filter.
        self.assertIn("min-w-0 ml-auto max-w-2xl px-4 py-3 rounded-default text-white w-fit", html)
        self.assertIn("primera linea<br>segunda linea", html)
        self.assertNotIn("whitespace-pre-line", html)
        self.assertNotIn("items-end", html)
        # break-words keeps an unbroken run of characters inside the bubble,
        # hyphens:auto marks a split ordinary word with a hyphen.
        self.assertEqual(html.count('<p class="break-words'), 1)
        self.assertIn('style="hyphens: auto;"', html)

    def test_long_thread_scrolls_with_the_newest_message_last(self):
        for n in range(1, 8):
            self._write(self.approver, body=f"mensaje {n}", recipient=self.requester)
        self.client.force_login(self.requester)

        html = self.client.get(self._change_url()).content.decode()

        # Newest first in the DOM inside a column-reverse box: that renders
        # oldest-to-newest on screen and parks the scroll on the newest.
        self.assertIn("flex flex-col-reverse", html)
        self.assertIn("overflow-y-auto", html)
        self.assertEqual(
            re.findall(r"mensaje \d", html), [f"mensaje {n}" for n in range(7, 0, -1)]
        )

    def test_add_page_opens_with_the_conversation_inline(self):
        # The add view hands the inline an unsaved order, which has no
        # messages relation to follow.
        adder = self._make_user("adder", self.company)
        adder.user_permissions.add(
            Permission.objects.get(
                codename="add_purchaseorder", content_type__app_label="purchasing"
            )
        )
        self.client.force_login(adder)

        response = self.client.get(reverse("admin:purchasing_purchaseorder_add"))

        self.assertEqual(response.status_code, 200)

    def test_conversation_tab_is_rendered_for_the_requester(self):
        self.client.force_login(self.requester)

        response = self.client.get(self._change_url())

        # The two contracts the tab depends on: the tab bar link and the pane
        # id Unfold's JS looks up to reopen the tab after a redirect.
        self.assertContains(response, 'href="#messages"')
        self.assertContains(response, 'id="messages-group"')
        self.assertContains(response, 'id="po-message-form"')
        # Sending must not trip Unfold's unsaved-changes prompt.
        self.assertContains(response, 'removeEventListener("beforeunload"')


class POMessageUnreadTests(POMessageTestBase):
    """Read state marks the messages that arrived since the reader's last
    visit. Nothing else consumes it: the tab shows a plain message count and
    the changelist shows nothing at all."""

    def _inline_request(self, user):
        request = RequestFactory().get("/")
        request.user = user
        return request

    def test_tab_shows_a_plain_message_count(self):
        self._write(self.approver, recipient=self.requester)
        inline = PurchaseOrderMessageInline(PurchaseOrder, admin.site)

        self.assertEqual(inline.get_count(self._inline_request(self.requester), self.po), 1)

    def test_new_messages_are_flagged_on_the_first_visit_only(self):
        self._write(self.approver, body="pregunta", recipient=self.requester)
        self.client.force_login(self.requester)

        first = self.client.get(self._change_url()).content.decode()
        second = self.client.get(self._change_url()).content.decode()

        # The marker itself, not the "Nuevo mensaje" label on the compose box.
        marker = 'dark:text-red-400">Nuevo</span>'
        self.assertIn(marker, first)
        self.assertNotIn(marker, second)

    def test_opening_the_order_marks_its_messages_as_read(self):
        message = self._write(self.approver, recipient=self.requester)
        own = self._write(self.requester, recipient=self.approver)

        self.client.force_login(self.requester)
        self.client.get(self._change_url())

        message.refresh_from_db()
        own.refresh_from_db()
        self.assertIsNotNone(message.read_at)
        self.assertIsNone(own.read_at)  # the recipient has not opened it yet
