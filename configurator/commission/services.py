from decimal import Decimal
from djmoney.money import Money
from django.db import transaction
from django.db.models import Sum


class CommissionService:

    @classmethod
    @transaction.atomic
    def prepopulate_for_quote(cls, quote) -> None:
        """
        Creates one QuoteCommission row for the quote's agent,
        if one does not already exist.
        """
        from configurator.commission.models import QuoteCommission

        if not quote.agent_id:
            return

        QuoteCommission.objects.get_or_create(
            quote=quote,
            agent=quote.agent,
        )

    @classmethod
    @transaction.atomic
    def recalculate(cls, quote) -> None:
        """
        Recalculates commission_amount for every QuoteCommission row
        belonging to this quote.
        Commission is calculated on the quote basket (subtotal, discounted_subtotal, total).
        """
        from configurator.commission.models import AgentCommissionRule, QuoteCommission

        rows = list(
            QuoteCommission.objects.filter(
                quote=quote
            ).select_related("agent", "rule")
        )

        if not rows:
            return

        # Calculate total quantity across all items on the quote
        def quantity_of(related, field="quantity"):
            return Decimal(str(related.aggregate(total=Sum(field))["total"] or 0))

        total_quantity = (
            quantity_of(quote.configurations, "items__quantity")
            + quantity_of(quote.inventory_items)
            + quantity_of(quote.items)
        )

        subtotal = Decimal(str(quote.subtotal_price.amount))
        discounted_subtotal = Decimal(str(quote.discounted_subtotal.amount)) if quote.discounted_subtotal else subtotal
        total = Decimal(str(quote.total_price.amount))

        changed = False
        for row in rows:
            if not row.agent_id:
                continue
            previous_amount = row.commission_amount.amount

            try:
                rule = AgentCommissionRule.objects.get(
                    agent_id=row.agent_id,
                    is_active=True,
                )
                row.rule = rule
                row.rule_type_snapshot = rule.rule_type
                row.rule_value_snapshot = rule.value
                amount = rule.compute_commission(
                    subtotal, discounted_subtotal, total, total_quantity,
                )

            except AgentCommissionRule.DoesNotExist:
                # No rule defined — set commission to zero, leave rule blank
                amount = Decimal("0.00")
                row.rule = None
                row.rule_type_snapshot = ""
                row.rule_value_snapshot = None

            row.commission_amount = amount
            changed = changed or amount != previous_amount
            row.save(update_fields=[
                "rule",
                "rule_type_snapshot",
                "rule_value_snapshot",
                "commission_amount",
                "calculated_at",
            ])

        if changed:
            CommissionReleaseService.sync_for_quote(quote)

    @classmethod
    def refresh(cls, quote) -> None:
        """Make sure the quote's agent has a commission row and it is up to date."""
        cls.prepopulate_for_quote(quote)
        cls.recalculate(quote)

    @classmethod
    def recalculate_unruled_for_agent(cls, agent_id) -> None:
        """
        Recalculate the agent's quotes that were calculated before the agent
        had a rule (they hold 0.00). Quotes already calculated with a rule
        keep the amount of that rule.
        """
        from configurator.commission.models import QuoteCommission

        commissions = QuoteCommission.objects.filter(
            agent_id=agent_id, rule__isnull=True, quote__isnull=False,
        ).select_related("quote")
        for commission in commissions:
            cls.recalculate(commission.quote)

    @classmethod
    def get_commission_context(cls, quote) -> dict:
        """
        Returns commission data for quote export templates.
        """
        from configurator.commission.models import QuoteCommission

        rows = (
            QuoteCommission.objects
            .filter(quote=quote)
            .select_related("agent", "rule")
            .order_by("agent__name")
        )

        result = []
        changed = False
        for row in rows:
            if not row.agent_id:
                continue
            previous_amount = row.commission_amount.amount
            result.append({
                "agent_name": row.agent.name,
                "rule_type": row.rule_type_snapshot,
                "rule_value": float(row.rule_value_snapshot) if row.rule_value_snapshot else None,
                "commission_amount": float(row.commission_amount.amount),
            })

        total = sum(r["commission_amount"] for r in result)
        return {
            "commissions": result,
            "total_commission": total,
            "has_commissions": bool(result),
        }

CENT = Decimal("0.01")


class CommissionReleaseService:
    """
    Releases a quote's commission as its invoices are collected.

    An invoice releases, for each quote it bills, the commission on the part
    of the quote's net base it carries, in proportion to what has been paid
    of its total: commission / quote net base × quote base on the invoice ×
    paid / invoice total. Advances release too: an advance line is split
    across its expediente's accepted quotes by their net base, and the
    deduction lines on the final invoice subtract the same shares, so a
    project collected in full releases exactly its commission.
    """

    @classmethod
    @transaction.atomic
    def sync_for_invoice(cls, invoice) -> None:
        """Rebuild every release of ``invoice`` from its current allocations."""
        from django.contrib.contenttypes.models import ContentType
        from configurator.commission.models import CommissionRelease, QuoteCommission
        from invoices.models import SalesInvoiceStatus
        from payments.models import PaymentAllocation, PaymentStatus
        from sales.models import CommissionReleasePolicy

        CommissionRelease.objects.filter(sales_invoice=invoice).delete()

        total = Decimal(str(invoice.total_amount.amount))
        if invoice.status == SalesInvoiceStatus.CANCELLED or not total:
            return

        base_by_quote = cls.net_base_by_quote(invoice)
        if not base_by_quote:
            return

        commissions = (
            QuoteCommission.objects
            .filter(quote_id__in=base_by_quote, agent__isnull=False)
            .select_related("agent", "quote")
        )
        allocations = None
        releases = []
        for commission in commissions:
            quote = commission.quote
            quote_base = Decimal(str((quote.discounted_subtotal or quote.subtotal_price).amount))
            if not quote_base:
                continue
            base = base_by_quote[commission.quote_id]
            amount = Decimal(str(commission.commission_amount.amount)) * base / quote_base
            currency = commission.commission_amount_currency

            if commission.agent.release_policy == CommissionReleasePolicy.ON_INVOICE:
                if invoice.status == SalesInvoiceStatus.FINAL:
                    releases.append(CommissionRelease(
                        quote_commission=commission,
                        sales_invoice=invoice,
                        release_date=invoice.date_issued,
                        base_amount=Money(base.quantize(CENT), currency),
                        released_amount=Money(amount.quantize(CENT), currency),
                    ))
                continue

            if allocations is None:
                allocations = list(
                    PaymentAllocation.objects
                    .filter(
                        content_type=ContentType.objects.get_for_model(invoice),
                        object_id=invoice.pk,
                    )
                    .exclude(payment__status=PaymentStatus.CANCELLED)
                    .select_related("payment")
                    .order_by("payment__payment_date", "pk")
                )
            # Rounded on the running total so the cents add up to the commission
            paid = Decimal("0")
            released = Decimal("0")
            for allocation in allocations:
                applied = Decimal(str(allocation.amount_applied.amount))
                paid += applied
                share = (amount * paid / total).quantize(CENT) - released
                released += share
                releases.append(CommissionRelease(
                    quote_commission=commission,
                    sales_invoice=invoice,
                    allocation=allocation,
                    release_date=allocation.payment.payment_date,
                    base_amount=Money((base * applied / total).quantize(CENT), currency),
                    released_amount=Money(share, currency),
                ))

        CommissionRelease.objects.bulk_create(releases)

    @classmethod
    def sync_for_quote(cls, quote) -> None:
        """Rebuild the releases of every invoice that bills ``quote``."""
        from invoices.models import SalesInvoice

        invoice_ids = set(
            SalesInvoice.objects
            .filter(commission_releases__quote_commission__quote=quote)
            .values_list("pk", flat=True)
        )
        invoice_ids |= cls._invoice_ids_billing(quote)
        for invoice in SalesInvoice.objects.filter(pk__in=invoice_ids):
            cls.sync_for_invoice(invoice)

    @classmethod
    def net_base_by_quote(cls, invoice) -> dict:
        """
        {quote_id: net base of the quote this invoice carries}, after the
        invoice's header discount. Lines with no quote (work entries, direct
        configurations) carry none.
        """
        from invoices import line_sources

        lines = [
            (item.source_id or "", Decimal(str(item.line_total.amount)))
            for item in invoice.items.all()
        ]
        lines_total = sum((amount for _, amount in lines), Decimal("0"))
        if not lines_total:
            return {}

        quote_of = line_sources.quote_ids_by_source([source for source, _ in lines])
        advance_pf_of = cls._advance_project_files_of([source for source, _ in lines])
        weights_of_pf = {}

        base_by_quote = {}
        for source, amount in lines:
            prefix, pk = line_sources.parse(source)
            if prefix == line_sources.ADVANCE_LINE:
                pf_id = pk
            elif prefix == line_sources.ADVANCE_DEDUCTION:
                pf_id = advance_pf_of.get(pk)
            else:
                quote_id = quote_of.get(source)
                if quote_id:
                    base_by_quote[quote_id] = base_by_quote.get(quote_id, Decimal("0")) + amount
                continue

            if pf_id is None:
                continue
            if pf_id not in weights_of_pf:
                weights_of_pf[pf_id] = cls._quote_weights(pf_id)
            for quote_id, weight in weights_of_pf[pf_id].items():
                base_by_quote[quote_id] = base_by_quote.get(quote_id, Decimal("0")) + amount * weight

        discount = Decimal(str(invoice.discount_amount.amount)) if invoice.discount_amount else Decimal("0")
        factor = (lines_total - discount) / lines_total
        return {quote_id: base * factor for quote_id, base in base_by_quote.items() if base}

    @staticmethod
    def _advance_project_files_of(sources) -> dict:
        """{advance line pk: its project file} for the deduction lines among ``sources``."""
        from invoices import line_sources
        from invoices.models import SalesInvoiceItem

        line_ids = {line_sources.pk_of(s, line_sources.ADVANCE_DEDUCTION) for s in sources} - {None}
        if not line_ids:
            return {}
        return {
            pk: line_sources.pk_of(source_id, line_sources.ADVANCE_LINE)
            for pk, source_id in SalesInvoiceItem.objects.filter(pk__in=line_ids).values_list("pk", "source_id")
        }

    @staticmethod
    def _quote_weights(project_file_id) -> dict:
        """
        {quote_id: share of the expediente's net base}, the same base an
        advance is taken on (accepted quotes plus direct configurations).
        """
        from management.models import ProjectFile
        from invoices.services.project_file_invoice_service import ProjectFileInvoiceService

        project_file = ProjectFile.objects.filter(pk=project_file_id).first()
        if project_file is None:
            return {}
        summary = ProjectFileInvoiceService.get_invoiceable_summary(project_file)
        net_base = summary["total_invoiceable_subtotal_net"]
        if not net_base:
            return {}
        weights = {}
        for quote_data in summary["quotes"]:
            quote = quote_data["quote"]
            quote_base = (
                Decimal(str(quote.discounted_subtotal.amount))
                if quote.discounted_subtotal else quote_data["subtotal"]
            )
            weights[quote.pk] = quote_base / net_base
        return weights

    @staticmethod
    def _invoice_ids_billing(quote) -> set:
        """Invoices with a line from ``quote`` or an advance on its expediente."""
        from django.db.models import Q
        from invoices import line_sources
        from invoices.models import SalesInvoiceItem

        lines = Q(source_id__in=line_sources.quote_source_ids(quote))
        if quote.project_file_id:
            advance_source = line_sources.source_id(line_sources.ADVANCE_LINE, quote.project_file_id)
            advance_line_ids = SalesInvoiceItem.objects.filter(
                source_id=advance_source
            ).values_list("pk", flat=True)
            lines |= Q(source_id=advance_source) | Q(source_id__in=[
                line_sources.source_id(line_sources.ADVANCE_DEDUCTION, pk) for pk in advance_line_ids
            ])
        return set(SalesInvoiceItem.objects.filter(lines).values_list("invoice_id", flat=True))
