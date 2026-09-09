"""
Unmatched PO Digest — email construction.

Turns the raw digest data (invoices.services.unmatched_po_digest_service)
into ready-to-send emails: recipient grouping/consolidation, display
formatting, per-invoice links straight to the PO matching screen,
template rendering and the subject.

Transport stays in core.services.email_service.EmailService; scheduling and
delivery bookkeeping in invoices.tasks.send_unmatched_po_digest.
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
from invoices.services.unmatched_po_digest_service import (
    CompanyUnmatchedDigest,
    UnmatchedPODigestService,
)

_ZERO = Decimal("0.00")


@dataclass(frozen=True)
class UnmatchedDigestEmail:
    """A ready-to-send digest email (transport-agnostic)."""

    recipients: list[str]
    subject: str
    body_html: str
    company: Company | None  # EmailLog attribution; None for consolidated emails


class UnmatchedPODigestEmailService:
    """Builds the daily unmatched-PO digest emails."""

    MAX_ROWS_PER_COMPANY = 25
    TEMPLATE = "invoices/email/unmatched_po_digest.html"

    @classmethod
    def build_emails(
        cls,
        companies: Iterable[Company],
        digests: dict[int, CompanyUnmatchedDigest],
        today: date,
    ) -> list[UnmatchedDigestEmail]:
        """Build one email per recipient group, skipping empty digests.

        Recipients configured on several companies get ONE consolidated
        email covering all of them (same behaviour as the pending-invoices
        digest). A group whose companies have nothing to report gets no
        email at all — a daily "all clear" would train people to ignore it.
        """
        emails = []
        for company_ids, recipients in cls._group_recipients(companies).items():
            group_digests = [digests[pk] for pk in company_ids]
            if not any(digest.count for digest in group_digests):
                continue
            emails.append(cls._build_email(group_digests, recipients, today))
        return emails

    # ── Email assembly ────────────────────────────────────────────────

    @classmethod
    def _build_email(
        cls,
        group_digests: list[CompanyUnmatchedDigest],
        recipients: list[str],
        today: date,
    ) -> UnmatchedDigestEmail:
        reported = [digest for digest in group_digests if digest.count]
        names = ", ".join(digest.company.name for digest in reported)
        single_company = reported[0].company if len(reported) == 1 else None
        total_count = sum(digest.count for digest in reported)

        body_html = render_to_string(
            cls.TEMPLATE,
            {
                "companies": [cls._company_context(digest) for digest in reported],
                "total_count": total_count,
                "total_display": cls._format_totals(
                    UnmatchedPODigestService.merge_totals(
                        digest.totals for digest in reported
                    )
                ),
                "report_date": date_format(today, "DATE_FORMAT"),
            },
        )
        return UnmatchedDigestEmail(
            recipients=recipients,
            subject=(
                f"Facturas sin orden de compra vinculada ({total_count}): {names}"
            ),
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
            for address in company.unmatched_po_digest_emails:
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

    # ── Links ─────────────────────────────────────────────────────────

    @staticmethod
    def _match_url(invoice_pk: int) -> str:
        """Link straight to that invoice's PO matching screen."""
        base_url = settings.BACKEND_URL.rstrip("/")
        path = reverse("admin:invoices_invoice_match_po", args=[invoice_pk])
        return f"{base_url}{path}"

    @staticmethod
    def _changelist_url(company: Company) -> str:
        base_url = settings.BACKEND_URL.rstrip("/")
        path = reverse("admin:invoices_invoice_changelist")
        return f"{base_url}{path}?company__id__exact={company.pk}"

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

    @staticmethod
    def _format_age(age_hours: int) -> str:
        """Human-readable age: hours under two days, days beyond that."""
        if age_hours < 48:
            return f"{age_hours} h"
        return f"{age_hours // 24} días"

    @classmethod
    def _company_context(cls, digest: CompanyUnmatchedDigest) -> dict:
        """Map a raw CompanyUnmatchedDigest to the email template context."""
        return {
            "name": digest.company.name,
            "count": digest.count,
            "partial_count": digest.partial_count,
            "grace_hours": digest.grace_hours,
            "rows": [
                {
                    "provider": row.provider,
                    "number": row.number,
                    "created_at": date_format(
                        row.created_at, "SHORT_DATE_FORMAT"
                    ),
                    "age": cls._format_age(row.age_hours),
                    "status": "Parcial" if row.is_partial else "Sin vincular",
                    "is_partial": row.is_partial,
                    "uncovered": cls._format_money(row.uncovered_amount, row.currency),
                    "url": cls._match_url(row.invoice.pk),
                }
                for row in digest.rows[: cls.MAX_ROWS_PER_COMPANY]
            ],
            "extra_count": max(0, digest.count - cls.MAX_ROWS_PER_COMPANY),
            "total_display": cls._format_totals(digest.totals),
            "url": cls._changelist_url(digest.company),
        }
