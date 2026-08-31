"""
Pending Invoices Digest — email construction.

Turns the raw digest data (invoices.services.pending_invoices_digest_service)
into ready-to-send emails: recipient grouping/consolidation, display
formatting, pre-filtered admin links, template rendering and the subject.

Transport stays in core.services.email_service.EmailService; scheduling and
delivery bookkeeping in invoices.tasks.send_pending_invoices_digest.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Iterable

from django.conf import settings
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils.formats import date_format, number_format

from company.models import Company
from invoices.models import SalesInvoiceStatus
from invoices.services.pending_invoices_digest_service import (
    CompanyDigest,
    DigestSection,
    PendingInvoicesDigestService,
)

_ZERO = Decimal("0.00")


@dataclass(frozen=True)
class DigestEmail:
    """A ready-to-send digest email (transport-agnostic)."""

    recipients: list[str]
    subject: str
    body_html: str
    company: Company | None  # EmailLog attribution; None for consolidated emails


class PendingInvoicesDigestEmailService:
    """Builds the daily pending-invoices digest emails."""

    MAX_ROWS_PER_SECTION = 10
    TEMPLATE = "invoices/email/pending_invoices_digest.html"
    PENDING_PARAMS = "payment_status__in=unpaid,partial"

    @classmethod
    def build_emails(
        cls,
        companies: Iterable[Company],
        digests: dict[int, CompanyDigest],
        today: date,
    ) -> list[DigestEmail]:
        """Build one email per recipient group.

        Recipients configured on several companies get ONE consolidated
        email covering all of them; recipients subscribed to the exact
        same set of companies share a single batched email (same
        behaviour as banking.tasks.send_bank_accounts_summary).
        """
        return [
            cls._build_email([digests[pk] for pk in company_ids], recipients, today)
            for company_ids, recipients in cls._group_recipients(companies).items()
        ]

    # ── Email assembly ────────────────────────────────────────────────

    @classmethod
    def _build_email(
        cls, group_digests: list[CompanyDigest], recipients: list[str], today: date
    ) -> DigestEmail:
        names = ", ".join(d.company.name for d in group_digests)
        single_company = group_digests[0].company if len(group_digests) == 1 else None

        body_html = render_to_string(
            cls.TEMPLATE,
            {
                "companies": [
                    {
                        "name": d.company.name,
                        "payables": cls._section_context(
                            d.payables, cls._payables_url(d.company)
                        ),
                        "receivables": cls._section_context(
                            d.receivables, cls._receivables_url(d.company)
                        ),
                    }
                    for d in group_digests
                ],
                "summary_lines": cls._summary_lines(group_digests),
                "report_date": date_format(today, "DATE_FORMAT"),
            },
        )
        return DigestEmail(
            recipients=recipients,
            subject=f"Resumen diario de facturas pendientes: {names}",
            body_html=body_html,
            company=single_company,
        )

    # ── Recipient grouping ────────────────────────────────────────────

    @staticmethod
    def _group_recipients(
        companies: Iterable[Company],
    ) -> dict[tuple[int, ...], list[str]]:
        """Group recipients by the exact set of companies they subscribe to."""
        recipient_company_ids: dict[str, dict] = {}  # normalized email -> entry
        for company in companies:
            for address in company.invoice_digest_emails:
                normalized = address.strip().lower()
                if not normalized:
                    continue
                entry = recipient_company_ids.setdefault(
                    normalized, {"email": address.strip(), "company_ids": []}
                )
                if company.pk not in entry["company_ids"]:
                    entry["company_ids"].append(company.pk)

        groups: dict[tuple[int, ...], list[str]] = {}
        for entry in recipient_company_ids.values():
            groups.setdefault(tuple(entry["company_ids"]), []).append(entry["email"])
        return groups

    # ── Pre-filtered admin links ──────────────────────────────────────

    @classmethod
    def _payables_url(cls, company: Company) -> str:
        base_url = settings.BACKEND_URL.rstrip("/")
        path = reverse("admin:invoices_invoice_changelist")
        return f"{base_url}{path}?company__id__exact={company.pk}&{cls.PENDING_PARAMS}"

    @classmethod
    def _receivables_url(cls, company: Company) -> str:
        base_url = settings.BACKEND_URL.rstrip("/")
        path = reverse("admin:invoices_salesinvoice_changelist")
        return (
            f"{base_url}{path}?company__id__exact={company.pk}"
            f"&status__exact={SalesInvoiceStatus.FINAL}&{cls.PENDING_PARAMS}"
        )

    # ── Display formatting ────────────────────────────────────────────

    @staticmethod
    def _format_money(amount: Decimal, currency: str = "EUR") -> str:
        formatted = number_format(amount, 2, force_grouping=True)
        return f"{formatted} €" if currency == "EUR" else f"{formatted} {currency}"

    @classmethod
    def _format_totals(cls, totals: dict[str, Decimal]) -> str:
        """Render a {currency: Decimal} dict as a display string (EUR first)."""
        if not totals:
            return cls._format_money(_ZERO)
        currencies = sorted(totals, key=lambda c: (c != "EUR", c))
        return " + ".join(cls._format_money(totals[c], c) for c in currencies)

    @classmethod
    def _section_context(cls, section: DigestSection, url: str) -> dict:
        """Map a raw DigestSection to the email template context."""
        return {
            "count": section.count,
            "rows": [
                {
                    "counterparty": row.counterparty,
                    "number": row.number,
                    "due_date": date_format(row.due_date, "SHORT_DATE_FORMAT")
                    if row.due_date
                    else "—",
                    "remaining": cls._format_money(row.remaining, row.currency),
                    "overdue": row.overdue,
                }
                for row in section.rows[: cls.MAX_ROWS_PER_SECTION]
            ],
            "extra_count": max(0, section.count - cls.MAX_ROWS_PER_SECTION),
            "total_display": cls._format_totals(section.totals),
            "overdue_count": section.overdue_count,
            "overdue_display": cls._format_totals(section.overdue_totals),
            "url": url,
        }

    @classmethod
    def _summary_lines(cls, group_digests: list[CompanyDigest]) -> list[dict]:
        """Consolidated to-pay / to-receive / net lines, one per currency."""
        pay_totals = PendingInvoicesDigestService.merge_totals(
            d.payables.totals for d in group_digests
        )
        receive_totals = PendingInvoicesDigestService.merge_totals(
            d.receivables.totals for d in group_digests
        )
        currencies = sorted(
            set(pay_totals) | set(receive_totals), key=lambda c: (c != "EUR", c)
        ) or ["EUR"]

        lines = []
        for currency in currencies:
            to_pay = pay_totals.get(currency, _ZERO)
            to_receive = receive_totals.get(currency, _ZERO)
            lines.append({
                "currency": currency,
                "to_pay": cls._format_money(to_pay, currency),
                "to_receive": cls._format_money(to_receive, currency),
                "net": cls._format_money(to_receive - to_pay, currency),
            })
        return lines
