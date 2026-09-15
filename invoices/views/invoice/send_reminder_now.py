from datetime import date
from uuid import uuid4

from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.translation import gettext as _
from django.views import View
from unfold.views import UnfoldModelAdminViewMixin

from invoices.models import (
    SalesInvoice,
    SalesInvoiceStatus,
    InvoicePaymentStatus,
    PaymentReminderLog,
    PaymentReminderLogStatus,
    PaymentReminderType,
    PaymentReminderTriggeredBy,
    resolve_reminder_language,
)
from invoices.tasks import send_payment_reminder_email, send_consolidated_payment_reminder_email


class SendPaymentReminderNowAdminView(UnfoldModelAdminViewMixin, View):
    """
    Confirmation screen for POL-xxx's manual "send reminder now" action.
    Two independent sections on one page, each with its own form:

      - Single: reminder for the one invoice this page was opened from.
        Reminder type, day count, recipient and language are all derived
        from the invoice itself.
      - Consolidated: pick any subset of this client's pending invoices
        and bundle them into one email. Manual only -- never triggered by
        the automatic scheduler -- so it deliberately does NOT hard-block
        on a given invoice's own "Exclude From Payment Reminders" flag: a
        human explicitly checking that invoice here is an intentional
        override, surfaced as a warning rather than a block. The
        client-level "Do Not Send Payment Reminders" flag still blocks
        the whole page -- that one is a broader, standing decision.
    """
    permission_required = ("invoices.change_salesinvoice",)
    title = "Send Payment Reminder Now"

    def _page_blocked_reason(self, client):
        """Blocks BOTH sections -- a client-wide decision, not per-invoice."""
        if client.exclude_from_payment_reminders:
            return _("This client is marked \"Do Not Send Payment Reminders\".")
        if not client.payment_reminder_recipients():
            return _("This client has no email address on file.")
        return None

    def _single_blocked_reason(self, invoice, client):
        """Blocks only the single-invoice section, for this invoice specifically."""
        page_blocked = self._page_blocked_reason(client)
        if page_blocked:
            return page_blocked
        if invoice.payment_status == InvoicePaymentStatus.PAID:
            return _("This invoice has already been paid.")
        if invoice.exclude_from_payment_reminders:
            return _("This invoice is marked \"Exclude From Payment Reminders\".")
        if not invoice.due_date:
            return _("This invoice has no due date set.")
        return None

    def _client_invoice_rows(self, client, company):
        """
        All FINAL, unpaid invoices for this client (any due date -- no
        "near due date" filtering), for the consolidated checklist.
        Excluded invoices are included, not hidden -- flagged with a
        warning instead, since this manual flow lets a human override
        that flag deliberately.
        """
        invoices = (
            SalesInvoice.objects.filter(
                company=company, client=client, status=SalesInvoiceStatus.FINAL,
            )
            .exclude(payment_status=InvoicePaymentStatus.PAID)
            .order_by("due_date")
        )
        rows = []
        for inv in invoices:
            days_diff = (inv.due_date - date.today()).days if inv.due_date else None
            rows.append({
                "invoice": inv,
                "display_number": inv.invoice_number or f"Pro Forma #{inv.pk}",
                "is_overdue": days_diff is not None and days_diff < 0,
                "days_count": abs(days_diff) if days_diff is not None else None,
                "is_excluded": inv.exclude_from_payment_reminders,
                "checked_default": not inv.exclude_from_payment_reminders,
            })
        return rows

    def _build_context(self, request, invoice):
        client = invoice.client
        page_blocked_reason = self._page_blocked_reason(client)
        single_blocked_reason = self._single_blocked_reason(invoice, client)

        days_diff = None
        is_overdue = False
        if invoice.due_date:
            days_diff = (invoice.due_date - date.today()).days
            is_overdue = days_diff < 0

        return {
            **self.model_admin.admin_site.each_context(request),
            "invoice": invoice,
            "client": client,
            "recipients": client.payment_reminder_recipients(),
            "page_blocked_reason": page_blocked_reason,
            "blocked_reason": single_blocked_reason,
            "days_count": abs(days_diff) if days_diff is not None else None,
            "is_overdue": is_overdue,
            "language": resolve_reminder_language(invoice),
            "title": f"{_('Send Payment Reminder Now')}: {invoice.invoice_number}",
            "client_invoice_rows": [] if page_blocked_reason else self._client_invoice_rows(client, invoice.company),
        }

    def get(self, request, object_id):
        invoice = get_object_or_404(
            SalesInvoice.objects.select_related("client", "company"), pk=object_id
        )
        context = self._build_context(request, invoice)
        return render(request, "admin/invoices/salesinvoice/send_reminder_now.html", context)

    def post(self, request, object_id):
        invoice = get_object_or_404(
            SalesInvoice.objects.select_related("client", "company"), pk=object_id
        )
        client = invoice.client
        back_url = (
            reverse("admin:invoices_salesinvoice_change", args=[invoice.pk])
            + "#payment_reminder_logs"
        )
        action = request.POST.get("action")

        if action == "consolidated":
            return self._post_consolidated(request, invoice, client, back_url)
        return self._post_single(request, invoice, client, back_url)

    def _post_single(self, request, invoice, client, back_url):
        blocked_reason = self._single_blocked_reason(invoice, client)
        if blocked_reason:
            self.model_admin.message_user(request, blocked_reason, level="error")
            return redirect(back_url)

        days_diff = (invoice.due_date - date.today()).days
        reminder_type = (
            PaymentReminderType.OVERDUE if days_diff < 0 else PaymentReminderType.PRE_DUE
        )

        log = PaymentReminderLog.objects.create(
            company=invoice.company,
            invoice=invoice,
            rule=None,
            reminder_type=reminder_type,
            triggered_by=PaymentReminderTriggeredBy.MANUAL,
            scheduled_for=date.today(),
        )
        send_payment_reminder_email.delay(log.id)

        self.model_admin.message_user(
            request,
            _("Payment reminder queued for %(number)s.") % {"number": invoice.invoice_number},
        )
        return redirect(back_url)

    def _post_consolidated(self, request, invoice, client, back_url):
        page_blocked_reason = self._page_blocked_reason(client)
        if page_blocked_reason:
            self.model_admin.message_user(request, page_blocked_reason, level="error")
            return redirect(back_url)

        selected_ids = request.POST.getlist("invoice_ids")
        if not selected_ids:
            self.model_admin.message_user(
                request, _("Select at least one invoice to send a consolidated reminder."), level="error"
            )
            return redirect(back_url)

        invoices = (
            SalesInvoice.objects.filter(
                pk__in=selected_ids, client=client, company=invoice.company,
                status=SalesInvoiceStatus.FINAL,
            )
            .exclude(payment_status=InvoicePaymentStatus.PAID)
            .exclude(due_date__isnull=True)
        )
        if not invoices.exists():
            self.model_admin.message_user(
                request, _("None of the selected invoices are eligible (unpaid, finalized, with a due date)."),
                level="error",
            )
            return redirect(back_url)

        batch_id = uuid4()
        log_ids = []
        for inv in invoices:
            days_diff = (inv.due_date - date.today()).days
            reminder_type = (
                PaymentReminderType.OVERDUE if days_diff < 0 else PaymentReminderType.PRE_DUE
            )
            log = PaymentReminderLog.objects.create(
                company=inv.company,
                invoice=inv,
                rule=None,
                reminder_type=reminder_type,
                triggered_by=PaymentReminderTriggeredBy.MANUAL,
                scheduled_for=date.today(),
                batch_id=batch_id,
            )
            log_ids.append(log.id)

        send_consolidated_payment_reminder_email.delay(log_ids)

        self.model_admin.message_user(
            request,
            _("Consolidated reminder queued for %(count)s invoice(s).") % {"count": len(log_ids)},
        )
        return redirect(back_url)