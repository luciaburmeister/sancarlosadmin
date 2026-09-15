"""
Payment Reminder Report — email construction.

Turns the day's PaymentReminderLog rows of one company into the report
email sent to the people who run collections: which clients were emailed
a payment reminder, for which invoice, and whether each email went
through (accepted by the mail server), failed (with the reason) or was
cancelled before sending (invoice paid or excluded in the meantime).

Transport stays in core.services.email_service.EmailService; scheduling
in invoices.tasks.send_payment_reminder_report.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Iterable

from django.conf import settings
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone
from django.utils.formats import date_format

from company.models import Company
from invoices.models import PaymentReminderLog, PaymentReminderLogStatus


@dataclass(frozen=True)
class ReportEmail:
    """A ready-to-send reminder report email (transport-agnostic)."""

    recipients: list[str]
    subject: str
    body_html: str
    company: Company


class PaymentReminderReportEmailService:
    """Builds the payment reminder report email for one company and day."""

    TEMPLATE = "invoices/email/payment_reminder_report.html"

    # Display order: problems first so they are visible without scrolling.
    STATUS_ORDER = {
        PaymentReminderLogStatus.FAILED: 0,
        PaymentReminderLogStatus.PENDING: 1,
        PaymentReminderLogStatus.SENT: 2,
        PaymentReminderLogStatus.CANCELLED: 3,
    }

    @classmethod
    def build_email(
        cls,
        company: Company,
        logs: Iterable[PaymentReminderLog],
        recipients: list[str],
        report_date: date,
    ) -> ReportEmail:
        logs = sorted(
            logs,
            key=lambda log: (
                cls.STATUS_ORDER.get(log.status, 99),
                log.invoice.client.name.lower(),
                log.invoice.invoice_number or "",
            ),
        )
        counts = cls._counts(logs)
        body_html = render_to_string(
            cls.TEMPLATE,
            {
                "company_name": company.name,
                "report_date": date_format(report_date, "DATE_FORMAT"),
                "rows": [cls._row_context(log) for log in logs],
                "counts": counts,
                "has_problems": bool(counts["failed"] or counts["pending"]),
                "logs_url": cls._logs_url(company, report_date),
            },
        )
        return ReportEmail(
            recipients=recipients,
            subject=cls._subject(company, counts, report_date),
            body_html=body_html,
            company=company,
        )

    # ── Subject ───────────────────────────────────────────────────────

    @staticmethod
    def _subject(company: Company, counts: dict[str, int], report_date: date) -> str:
        parts = [f"{counts['sent']} enviado{'s' if counts['sent'] != 1 else ''}"]
        if counts["failed"]:
            parts.append(f"{counts['failed']} fallido{'s' if counts['failed'] != 1 else ''}")
        if counts["cancelled"]:
            parts.append(f"{counts['cancelled']} cancelado{'s' if counts['cancelled'] != 1 else ''}")
        if counts["pending"]:
            parts.append(f"{counts['pending']} pendiente{'s' if counts['pending'] != 1 else ''}")
        return (
            f"Informe de recordatorios de pago {date_format(report_date, 'SHORT_DATE_FORMAT')}: "
            f"{company.name} ({', '.join(parts)})"
        )

    # ── Row / summary context ─────────────────────────────────────────

    @staticmethod
    def _counts(logs: list[PaymentReminderLog]) -> dict[str, int]:
        counts = {status.value: 0 for status in PaymentReminderLogStatus}
        for log in logs:
            counts[log.status] = counts.get(log.status, 0) + 1
        counts["total"] = len(logs)
        return counts

    @staticmethod
    def _row_context(log: PaymentReminderLog) -> dict:
        invoice = log.invoice
        client = invoice.client
        sent_at = (
            date_format(timezone.localtime(log.sent_at), "SHORT_DATETIME_FORMAT")
            if log.sent_at
            else "—"
        )
        return {
            "client": client.name,
            "email": ", ".join(client.payment_reminder_recipients()) or "—",
            "invoice_number": invoice.invoice_number or "—",
            "due_date": date_format(invoice.due_date, "SHORT_DATE_FORMAT")
            if invoice.due_date
            else "—",
            "reminder_type": log.get_reminder_type_display(),
            "triggered_by": log.get_triggered_by_display(),
            "status": log.status,
            "status_display": log.get_status_display(),
            "sent_at": sent_at,
            "detail": log.error_message or "",
        }

    # ── Pre-filtered admin link ───────────────────────────────────────

    @staticmethod
    def _logs_url(company: Company, report_date: date) -> str:
        base_url = settings.BACKEND_URL.rstrip("/")
        path = reverse("admin:invoices_paymentreminderlog_changelist")
        day = report_date.isoformat()
        return (
            f"{base_url}{path}?company__id__exact={company.pk}"
            f"&scheduled_for_from={day}&scheduled_for_to={day}"
        )
