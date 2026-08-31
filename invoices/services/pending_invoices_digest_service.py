"""
Pending Invoices Digest — data layer.

Builds the payables / receivables snapshot consumed by
invoices.tasks.send_pending_invoices_digest, and reusable by any future
dashboard or API endpoint.

Design rules:
- Raw data only: Decimals, dates and model instances. Presentation
  (number/date formatting, HTML, URLs) belongs to the caller.
- Batched: ONE query for all companies' payables and ONE for all
  receivables, regardless of how many companies are requested.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Iterable

from django.db.models import F
from django.utils import timezone

from company.models import Company
from invoices.models import (
    Invoice,
    InvoicePaymentStatus,
    SalesInvoice,
    SalesInvoiceStatus,
)
from payments.mixins import annotate_amount_remaining

_ZERO = Decimal("0.00")


@dataclass(frozen=True)
class DigestRow:
    """One pending document (purchase or sales invoice)."""

    counterparty: str
    number: str
    due_date: date | None
    remaining: Decimal
    currency: str
    overdue: bool


@dataclass(frozen=True)
class DigestSection:
    """One side of a company's digest: payables or receivables."""

    rows: list[DigestRow]
    totals: dict[str, Decimal]          # currency -> pending amount
    overdue_totals: dict[str, Decimal]  # currency -> pending amount past due
    overdue_count: int

    @property
    def count(self) -> int:
        return len(self.rows)


@dataclass(frozen=True)
class CompanyDigest:
    company: Company
    payables: DigestSection
    receivables: DigestSection


class PendingInvoicesDigestService:
    """Read-only reporting service for the pending-invoices digest.

    Payables: purchase invoices not fully paid (credit notes carry
    negative amounts, so they net the totals naturally).
    Receivables: final sales invoices not fully paid.
    """

    @classmethod
    def get_company_digests(
        cls, companies: Iterable[Company], today: date | None = None
    ) -> dict[int, CompanyDigest]:
        """Return {company_id: CompanyDigest} for the given companies."""
        companies = list(companies)
        today = today or timezone.localdate()
        company_ids = [company.pk for company in companies]

        payables_by_company = cls._group_by_company(
            annotate_amount_remaining(
                Invoice.objects.filter(company_id__in=company_ids)
                .exclude(payment_status=InvoicePaymentStatus.PAID)
                .select_related("provider"),
                Invoice,
            ).order_by(F("due_date").asc(nulls_last=True), "pk")
        )
        receivables_by_company = cls._group_by_company(
            annotate_amount_remaining(
                SalesInvoice.objects.filter(
                    company_id__in=company_ids, status=SalesInvoiceStatus.FINAL
                )
                .exclude(payment_status=InvoicePaymentStatus.PAID)
                .select_related("client"),
                SalesInvoice,
            ).order_by(F("due_date").asc(nulls_last=True), "pk")
        )

        return {
            company.pk: CompanyDigest(
                company=company,
                payables=cls._build_section(
                    payables_by_company.get(company.pk, []), "provider", today
                ),
                receivables=cls._build_section(
                    receivables_by_company.get(company.pk, []), "client", today
                ),
            )
            for company in companies
        }

    @staticmethod
    def merge_totals(totals_dicts: Iterable[dict[str, Decimal]]) -> dict[str, Decimal]:
        """Merge several {currency: amount} dicts (consolidated view)."""
        merged: dict[str, Decimal] = {}
        for totals in totals_dicts:
            for currency, amount in totals.items():
                merged[currency] = merged.get(currency, _ZERO) + amount
        return merged

    # ── Internals ─────────────────────────────────────────────────────

    @staticmethod
    def _group_by_company(queryset) -> dict[int, list]:
        grouped: dict[int, list] = {}
        for invoice in queryset:
            grouped.setdefault(invoice.company_id, []).append(invoice)
        return grouped

    @staticmethod
    def _build_section(invoices, counterparty_attr: str, today: date) -> DigestSection:
        rows = []
        totals: dict[str, Decimal] = {}
        overdue_totals: dict[str, Decimal] = {}
        overdue_count = 0

        for inv in invoices:
            remaining = inv.db_amount_remaining or _ZERO
            currency = getattr(inv, "total_amount_currency", None) or "EUR"
            totals[currency] = totals.get(currency, _ZERO) + remaining

            is_overdue = inv.due_date is not None and inv.due_date < today
            if is_overdue:
                overdue_count += 1
                overdue_totals[currency] = (
                    overdue_totals.get(currency, _ZERO) + remaining
                )

            counterparty = getattr(inv, counterparty_attr, None)
            rows.append(
                DigestRow(
                    counterparty=counterparty.name if counterparty else "—",
                    number=inv.invoice_number or f"#{inv.pk}",
                    due_date=inv.due_date,
                    remaining=remaining,
                    currency=currency,
                    overdue=is_overdue,
                )
            )

        return DigestSection(
            rows=rows,
            totals=totals,
            overdue_totals=overdue_totals,
            overdue_count=overdue_count,
        )
