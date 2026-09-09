"""
Unmatched PO Digest — data layer.

Builds the snapshot of purchase invoices whose lines are not fully
allocated to a purchase order, consumed by
invoices.tasks.send_unmatched_po_digest and reusable by any future
dashboard or API endpoint.

Design rules:
- Raw data only: Decimals, dates and model instances. Presentation
  (number/date formatting, HTML, URLs) belongs to the caller.
- ONE query regardless of how many companies are requested, since each
  company carries its own grace period and start date.

Invoices flagged skip_po_matching ("No relevant PO" — fuel, landfill
fees, bank charges) are exempt, as are credit notes, which reverse spend
rather than commit it.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Iterable

from django.db.models import DecimalField, F, OuterRef, Q, Subquery, Sum, Value
from django.db.models.functions import Coalesce
from django.utils import timezone

from company.models import Company
from invoices.models import (
    Invoice,
    InvoiceDocumentType,
    InvoiceItem,
    InvoicePOAllocation,
)

_ZERO = Decimal("0.00")
_QTY_FIELD = DecimalField(max_digits=10, decimal_places=3)


@dataclass(frozen=True)
class UnmatchedRow:
    """One purchase invoice still missing (part of) its PO link."""

    invoice: Invoice
    provider: str
    number: str
    created_at: datetime
    age_hours: int
    total_quantity: Decimal
    matched_quantity: Decimal
    uncovered_amount: Decimal
    currency: str

    @property
    def is_partial(self) -> bool:
        """True when some lines are matched but not all."""
        return self.matched_quantity > _ZERO


@dataclass(frozen=True)
class CompanyUnmatchedDigest:
    company: Company
    rows: list[UnmatchedRow]
    totals: dict[str, Decimal]  # currency -> uncovered amount
    grace_hours: int  # the threshold this company was measured against

    @property
    def count(self) -> int:
        return len(self.rows)

    @property
    def partial_count(self) -> int:
        return sum(1 for row in self.rows if row.is_partial)


class UnmatchedPODigestService:
    """Read-only reporting service for the unmatched-PO digest."""

    GRACE_HOURS = 48

    @classmethod
    def get_company_digests(
        cls,
        companies: Iterable[Company],
        now: datetime | None = None,
        grace_hours: int | None = None,
        start_date: date | None = None,
    ) -> dict[int, CompanyUnmatchedDigest]:
        """Return {company_id: CompanyUnmatchedDigest} for the given companies.

        Each company is measured against its own configured grace period and
        start date. `grace_hours` / `start_date` override those for every
        company; pass them only to preview a different threshold (dry runs,
        tests).
        """
        companies = list(companies)
        if not companies:
            return {}

        now = now or timezone.now()
        grace_by_company = {
            company.pk: cls._grace_hours_for(company, grace_hours)
            for company in companies
        }

        rows_by_company: dict[int, list[UnmatchedRow]] = {
            company.pk: [] for company in companies
        }
        for invoice in cls._unmatched_invoices(
            companies, now, grace_by_company, start_date
        ):
            rows_by_company.setdefault(invoice.company_id, []).append(
                cls._build_row(invoice, invoice.total_qty, invoice.matched_qty, now)
            )

        return {
            company.pk: CompanyUnmatchedDigest(
                company=company,
                rows=rows_by_company.get(company.pk, []),
                totals=cls._sum_totals(rows_by_company.get(company.pk, [])),
                grace_hours=grace_by_company[company.pk],
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

    @classmethod
    def _grace_hours_for(cls, company: Company, override: int | None) -> int:
        if override is not None:
            return override
        configured = company.unmatched_po_digest_grace_hours
        return cls.GRACE_HOURS if configured is None else configured

    @staticmethod
    def _company_scope(
        companies: list[Company],
        now: datetime,
        grace_by_company: dict[int, int],
        start_date: date | None,
    ) -> Q:
        """OR of each company's scope: its own grace period and start date."""
        scope = Q()
        for company in companies:
            cutoff = now - timezone.timedelta(hours=grace_by_company[company.pk])
            company_q = Q(company_id=company.pk, created_at__lt=cutoff)
            company_start = (
                start_date
                if start_date is not None
                else company.unmatched_po_digest_start_date
            )
            if company_start:
                company_q &= Q(created_at__date__gte=company_start)
            scope |= company_q
        return scope

    @classmethod
    def _unmatched_invoices(
        cls,
        companies: list[Company],
        now: datetime,
        grace_by_company: dict[int, int],
        start_date: date | None,
    ):
        """Invoices whose lines are not fully allocated to a PO.

        Subqueries rather than a joined Sum(), which double-counts lines
        with more than one allocation (as in InvoiceAdmin.get_queryset).
        """
        return (
            Invoice.objects.filter(
                cls._company_scope(companies, now, grace_by_company, start_date),
                skip_po_matching=False,
                document_type=InvoiceDocumentType.INVOICE,
            )
            .annotate(
                total_qty=cls._quantity_subquery(InvoiceItem),
                matched_qty=cls._quantity_subquery(InvoicePOAllocation),
            )
            # total_qty <= 0 means no lines were extracted: nothing is authorised.
            .filter(Q(total_qty__lte=_ZERO) | Q(matched_qty__lt=F("total_qty")))
            .select_related("provider")
            .order_by("created_at", "pk")
        )

    @staticmethod
    def _quantity_subquery(model) -> Coalesce:
        """Total `quantity` this invoice's rows carry in `model`, or zero."""
        totals = (
            model.objects.filter(invoice=OuterRef("pk"))
            .values("invoice")
            .annotate(total=Sum("quantity"))
            .values("total")
        )
        return Coalesce(
            Subquery(totals, output_field=_QTY_FIELD),
            Value(_ZERO),
            output_field=_QTY_FIELD,
        )

    @classmethod
    def _build_row(
        cls,
        invoice: Invoice,
        total_qty: Decimal,
        matched_qty: Decimal,
        now: datetime,
    ) -> UnmatchedRow:
        currency = getattr(invoice, "total_amount_currency", None) or "EUR"
        age_hours = int((now - invoice.created_at).total_seconds() // 3600)
        return UnmatchedRow(
            invoice=invoice,
            provider=invoice.provider.name if invoice.provider else "—",
            number=invoice.invoice_number or f"#{invoice.pk}",
            created_at=invoice.created_at,
            age_hours=age_hours,
            total_quantity=total_qty,
            matched_quantity=matched_qty,
            uncovered_amount=cls._uncovered_amount(invoice, total_qty, matched_qty),
            currency=currency,
        )

    @staticmethod
    def _uncovered_amount(
        invoice: Invoice, total_qty: Decimal, matched_qty: Decimal
    ) -> Decimal:
        """Invoice amount not yet covered by a PO allocation.

        Prorated by quantity, which is what the allocations track. Invoices
        whose lines were never extracted (total_qty == 0) count in full:
        nothing about them is authorised.
        """
        total_amount = invoice.total_amount.amount if invoice.total_amount else _ZERO
        if total_qty <= _ZERO:
            return total_amount
        uncovered_qty = max(_ZERO, total_qty - matched_qty)
        return (total_amount * uncovered_qty / total_qty).quantize(Decimal("0.01"))

    @staticmethod
    def _sum_totals(rows: list[UnmatchedRow]) -> dict[str, Decimal]:
        totals: dict[str, Decimal] = {}
        for row in rows:
            totals[row.currency] = totals.get(row.currency, _ZERO) + row.uncovered_amount
        return totals
