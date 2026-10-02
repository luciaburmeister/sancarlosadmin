from decimal import Decimal
from djmoney.money import Money
from django.db import transaction
from django.db.models import Sum


def commission_share(quote) -> Decimal:
    """
    The part of ``quote`` that earns commission: all of it but the
    configurations of products and the inventory lines of items marked
    "No commission" (portes, transport).
    """
    subtotal = Decimal(str(quote.subtotal_price.amount))
    if not subtotal:
        return Decimal("1")
    excluded_configs = quote.configurations.filter(
        product__exclude_from_commission=True,
    ).aggregate(total=Sum("subtotal_price"))["total"] or 0
    excluded_items = quote.inventory_items.filter(
        inventory_item__exclude_from_commission=True,
    ).aggregate(total=Sum("line_total"))["total"] or 0
    return (subtotal - Decimal(str(excluded_configs)) - Decimal(str(excluded_items))) / subtotal


class CommissionService:

    @classmethod
    @transaction.atomic
    def prepopulate_for_quote(cls, quote) -> bool:
        """
        Creates one QuoteCommission row for the quote's agent, if one does
        not already exist, and removes the rows of agents the quote no longer
        has (the agent was changed or cleared). Returns whether any was removed.
        """
        from configurator.commission.models import QuoteCommission

        stale = QuoteCommission.objects.filter(quote=quote)
        if quote.agent_id:
            stale = stale.exclude(agent_id=quote.agent_id)
        removed, _ = stale.delete()

        if quote.agent_id:
            QuoteCommission.objects.get_or_create(
                quote=quote,
                agent=quote.agent,
            )
        return bool(removed)

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
            quantity_of(quote.configurations.exclude(product__exclude_from_commission=True), "items__quantity")
            + quantity_of(quote.inventory_items.exclude(inventory_item__exclude_from_commission=True))
            + quantity_of(quote.items)
        )

        # Only the part that earns commission (no portes), after discounts
        share = commission_share(quote)
        subtotal = Decimal(str(quote.subtotal_price.amount))
        discounted_subtotal = Decimal(str(quote.discounted_subtotal.amount)) if quote.discounted_subtotal else subtotal
        total = Decimal(str(quote.total_price.amount))
        subtotal, discounted_subtotal, total = (
            (value * share).quantize(CENT) for value in (subtotal, discounted_subtotal, total)
        )

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
        removed = cls.prepopulate_for_quote(quote)
        cls.recalculate(quote)
        if removed:
            # The old agent's share leaves the invoices billing this quote
            CommissionReleaseService.sync_for_quote(quote)

    @classmethod
    def recalculate_unruled_for_agent(cls, agent_id) -> None:
        """
        Recalculate the agent's quotes and invoices that were calculated
        before the agent had a rule (they hold 0.00). Those already
        calculated with a rule keep the amount of that rule.
        """
        from configurator.commission.models import QuoteCommission

        commissions = QuoteCommission.objects.filter(
            agent_id=agent_id, rule__isnull=True, quote__isnull=False,
        ).select_related("quote")
        for commission in commissions:
            cls.recalculate(commission.quote)

        from configurator.commission.models import InvoiceCommission

        for invoice_commission in InvoiceCommission.objects.filter(
            agent_id=agent_id, rule__isnull=True,
        ).exclude(own_base_amount=0).select_related("sales_invoice"):
            CommissionReleaseService.sync_for_invoice(invoice_commission.sales_invoice)

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
        for row in rows:
            if not row.agent_id:
                continue
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
    Releases agents' commissions as their invoices are collected.

    An invoice releases, for each quote it bills, the commission on the part
    of the quote's net base it carries; the rest of its base (imported
    invoices, direct sales, work entries) carries the client's agent's own
    InvoiceCommission. Each part is released in proportion to what has been
    paid of the invoice's total: commission × paid / total. Advances release
    too: an advance line is split across its expediente's accepted quotes by
    their net base, and the deduction lines on the final invoice subtract
    the same shares, so a project collected in full releases exactly its
    commission.

    When it is released:
    - credit notes (negative total) take theirs back in full on issue;
    - agents released on issue get it in full when the invoice is final;
    - otherwise each payment allocated releases its share, dated by the
      payment; an invoice marked paid by hand, with no payment allocated,
      releases it in full on the day it was marked paid.
    """

    @classmethod
    @transaction.atomic
    def sync_for_invoice(cls, invoice) -> None:
        """Rebuild the commissions of ``invoice`` and their releases from its current state."""
        from configurator.commission.models import CommissionRelease, InvoiceCommission, QuoteCommission
        from invoices.models import SalesInvoiceStatus
        from sales.models import CommissionReleasePolicy

        CommissionRelease.objects.filter(sales_invoice=invoice).delete()
        rows = {row.agent_id: row for row in InvoiceCommission.objects.filter(sales_invoice=invoice)}

        total = Decimal(str(invoice.total_amount.amount))
        if invoice.status == SalesInvoiceStatus.CANCELLED or not total:
            InvoiceCommission.objects.filter(sales_invoice=invoice).delete()
            return

        base_by_quote, other_base, other_quantity = cls.net_base_by_quote(invoice)
        currency = invoice.currency or "EUR"

        # (agent, quote commission or None, base, amount)
        parts = []
        commissions = (
            QuoteCommission.objects
            .filter(quote_id__in=base_by_quote, agent__isnull=False)
            .select_related("agent", "quote")
        )
        for commission in commissions:
            quote = commission.quote
            quote_base = Decimal(str((quote.discounted_subtotal or quote.subtotal_price).amount)) * commission_share(quote)
            if not quote_base:
                continue
            base = base_by_quote[commission.quote_id]
            amount = Decimal(str(commission.commission_amount.amount)) * base / quote_base
            parts.append((commission.agent, commission, base, amount))

        own = cls._own_part(invoice, rows, other_base, other_quantity)
        if own is not None:
            agent, rule_fields, amount = own
            parts.append((agent, None, other_base, amount))

        # One row per agent: what the invoice earns them
        kept = {}
        for agent, _commission, base, amount in parts:
            row = kept.get(agent.pk) or rows.get(agent.pk) or InvoiceCommission(sales_invoice=invoice, agent=agent)
            if agent.pk not in kept:
                row.base_amount = Money(0, currency)
                row.commission_amount = Money(0, currency)
                row.own_base_amount = Money(0, currency)
                row.own_commission_amount = Money(0, currency)
                row.rule, row.rule_type_snapshot, row.rule_value_snapshot = None, "", None
                kept[agent.pk] = row
            row.base_amount += Money(base.quantize(CENT), currency)
            row.commission_amount += Money(amount.quantize(CENT), currency)
        if own is not None:
            agent, rule_fields, amount = own
            row = kept[agent.pk]
            row.own_base_amount = Money(other_base.quantize(CENT), currency)
            row.own_commission_amount = Money(amount.quantize(CENT), currency)
            for name, value in rule_fields.items():
                setattr(row, name, value)
        for agent_id, row in rows.items():
            if agent_id not in kept:
                row.delete()
        for row in kept.values():
            row.save()

        releases = []
        schedule = None
        for agent, commission, base, amount in parts:
            row = kept[agent.pk]
            if total < 0 or agent.release_policy == CommissionReleasePolicy.ON_INVOICE:
                # Credit notes and agents paid on issue: all of it, once final
                if invoice.status != SalesInvoiceStatus.FINAL:
                    continue
                steps = [(total, invoice.date_issued, None)]
            else:
                if schedule is None:
                    schedule = cls._payment_schedule(invoice, total)
                steps = schedule

            # Rounded on the running total so the cents add up to the commission
            paid = Decimal("0")
            released = Decimal("0")
            covered = Decimal("0")
            for applied, release_date, allocation in steps:
                paid += applied
                # An overpaid invoice releases its commission once, not more
                ratio = min(paid / total, Decimal("1"))
                share = (amount * ratio).quantize(CENT) - released
                base_share = (base * ratio).quantize(CENT) - covered
                released += share
                covered += base_share
                releases.append(CommissionRelease(
                    agent=agent,
                    invoice_commission=row,
                    quote_commission=commission,
                    sales_invoice=invoice,
                    allocation=allocation,
                    release_date=release_date,
                    base_amount=Money(base_share, currency),
                    released_amount=Money(share, currency),
                ))
        CommissionRelease.objects.bulk_create(releases)

        # What each row has released so far, and when it last did
        for row in kept.values():
            mine = [release for release in releases if release.invoice_commission is row]
            row.released_base_amount = Money(sum((r.base_amount.amount for r in mine), Decimal("0")), currency)
            row.released_amount = Money(sum((r.released_amount.amount for r in mine), Decimal("0")), currency)
            row.release_date = max((r.release_date for r in mine), default=None)
            row.save(update_fields=[
                "released_base_amount", "released_base_amount_currency",
                "released_amount", "released_amount_currency",
                "release_date", "calculated_at",
            ])

    @classmethod
    def _payment_schedule(cls, invoice, total) -> list:
        """
        [(amount paid, date, allocation)] of ``invoice``: its allocated
        payments, or the whole total on the day it was marked paid by hand.
        """
        from django.contrib.contenttypes.models import ContentType
        from invoices.models import InvoicePaymentStatus
        from payments.models import PaymentAllocation, PaymentStatus

        allocations = (
            PaymentAllocation.objects
            .filter(
                content_type=ContentType.objects.get_for_model(invoice),
                object_id=invoice.pk,
            )
            .exclude(payment__status=PaymentStatus.CANCELLED)
            .select_related("payment")
            .order_by("payment__payment_date", "pk")
        )
        schedule = [
            (Decimal(str(a.amount_applied.amount)), a.payment.payment_date, a)
            for a in allocations
        ]
        if not schedule and invoice.payment_status == InvoicePaymentStatus.PAID:
            schedule = [(total, cls.date_marked_paid(invoice), None)]
        return schedule

    @staticmethod
    def date_marked_paid(invoice):
        """
        The day ``invoice`` last became paid, from its history; its issue
        date when it was created already paid (imported) or has no history.
        """
        from django.utils import timezone
        from invoices.models import InvoicePaymentStatus

        became_paid = None
        previous = None
        for when, status in invoice.history.order_by("history_date", "history_id").values_list(
            "history_date", "payment_status",
        ):
            if status == InvoicePaymentStatus.PAID and previous not in (None, InvoicePaymentStatus.PAID):
                became_paid = when
            elif status != InvoicePaymentStatus.PAID:
                became_paid = None
            previous = status
        return timezone.localtime(became_paid).date() if became_paid else invoice.date_issued

    @classmethod
    def _own_part(cls, invoice, rows, base, quantity):
        """
        ``(agent, rule fields, amount)`` of the part of ``invoice`` no quote
        accounts for: the client's agent and their rule. Once the invoice is
        issued the agent and rule it was calculated with stay, unless it had
        no rule yet. None when that part is empty or there is no agent.
        """
        from configurator.commission.models import AgentCommissionRule
        from invoices.models import SalesInvoiceStatus
        from sales.models import CommissionistAgent

        if not base:
            return None
        frozen = next((row for row in rows.values() if row.own_base_amount.amount and row.rule_id), None)
        if frozen is not None and invoice.status != SalesInvoiceStatus.PROFORMA:
            return frozen.agent, {
                "rule": frozen.rule,
                "rule_type_snapshot": frozen.rule_type_snapshot,
                "rule_value_snapshot": frozen.rule_value_snapshot,
            }, Decimal(str(frozen.own_commission_amount.amount))

        agent_id = invoice.client.agent_id if invoice.client_id else None
        if not agent_id:
            return None
        agent = CommissionistAgent.objects.get(pk=agent_id)
        rule = AgentCommissionRule.objects.filter(agent_id=agent_id, is_active=True).first()
        if rule is None:
            return agent, {"rule": None, "rule_type_snapshot": "", "rule_value_snapshot": None}, Decimal("0")
        net_total = cls.net_total(invoice)
        total_share = Decimal(str(invoice.total_amount.amount)) * base / net_total if net_total else base
        return agent, {
            "rule": rule,
            "rule_type_snapshot": rule.rule_type,
            "rule_value_snapshot": rule.value,
        }, rule.compute_commission(base, base, total_share, quantity)

    @classmethod
    def sync_for_quote(cls, quote) -> None:
        """Rebuild the releases of every invoice that bills ``quote``."""
        from invoices.models import SalesInvoice

        invoice_ids = set(
            SalesInvoice.objects
            .filter(commission_releases__quote_commission__quote=quote)
            .distinct()
            .values_list("pk", flat=True)
        )
        invoice_ids |= cls._invoice_ids_billing(quote)
        for invoice in SalesInvoice.objects.filter(pk__in=invoice_ids):
            cls.sync_for_invoice(invoice)

    @classmethod
    def net_base_by_quote(cls, invoice):
        """
        ``({quote_id: net base of the quote this invoice carries}, the rest of
        its net base, the quantity of the lines in that rest)``, after the
        invoice's header discount. Lines with no quote (imported, work
        entries, direct configurations) form the rest.
        """
        from invoices import line_sources

        lines = [
            (item.source_id or "", Decimal(str(item.line_total.amount)), Decimal(str(item.quantity)))
            for item in invoice.items.all()
        ]
        lines_total = sum((amount for _, amount, _ in lines), Decimal("0"))
        if not lines_total:
            return {}, Decimal("0"), Decimal("0")

        quote_of = line_sources.quote_ids_by_source([source for source, _, _ in lines])
        no_commission = cls._no_commission_sources([source for source, _, _ in lines])
        advance_pf_of = cls._advance_project_files_of([source for source, _, _ in lines])
        weights_of_pf = {}

        base_by_quote = {}
        other_lines = Decimal("0")
        other_quantity = Decimal("0")
        for source, amount, quantity in lines:
            if source in no_commission:
                continue
            prefix, pk = line_sources.parse(source)
            if prefix == line_sources.ADVANCE_LINE:
                pf_id = pk
            elif prefix == line_sources.ADVANCE_DEDUCTION:
                pf_id = advance_pf_of.get(pk)
            else:
                quote_id = quote_of.get(source)
                if quote_id:
                    base_by_quote[quote_id] = base_by_quote.get(quote_id, Decimal("0")) + amount
                else:
                    other_lines += amount
                    other_quantity += quantity
                continue

            if pf_id is None:
                continue
            if pf_id not in weights_of_pf:
                weights_of_pf[pf_id] = cls._quote_weights(pf_id)
            weights, shares = weights_of_pf[pf_id]
            for quote_id, weight in weights.items():
                base_by_quote[quote_id] = (
                    base_by_quote.get(quote_id, Decimal("0")) + amount * weight * shares[quote_id]
                )
            # The share of the expediente no quote accounts for (direct configurations)
            other_lines += amount * (1 - sum(weights.values(), Decimal("0")))

        discount = Decimal(str(invoice.discount_amount.amount)) if invoice.discount_amount else Decimal("0")
        factor = (lines_total - discount) / lines_total
        base_by_quote = {quote_id: base * factor for quote_id, base in base_by_quote.items() if base}
        other_base = other_lines * factor
        if abs(other_base) < CENT:
            other_base = Decimal("0")
        return base_by_quote, other_base, other_quantity

    @staticmethod
    def net_total(invoice) -> Decimal:
        """The invoice's net (taxable) base: subtotal less its header discount."""
        discount = Decimal(str(invoice.discount_amount.amount)) if invoice.discount_amount else Decimal("0")
        return Decimal(str(invoice.subtotal_amount.amount)) - discount

    @staticmethod
    def _no_commission_sources(sources) -> set:
        """
        The source_ids among ``sources`` that earn no commission: lines of
        configurations of "No commission" products and of inventory lines of
        "No commission" items.
        """
        from configurator.models import Configuration
        from invoices import line_sources
        from sales.models import QuoteInventoryItem

        flagged = {
            line_sources.CONFIG: Configuration.objects.filter(product__exclude_from_commission=True),
            line_sources.DIRECT: Configuration.objects.filter(product__exclude_from_commission=True),
            line_sources.INV_ITEM: QuoteInventoryItem.objects.filter(inventory_item__exclude_from_commission=True),
        }
        pks_by_prefix = {}
        for source in sources:
            prefix, pk = line_sources.parse(source)
            if prefix in flagged:
                pks_by_prefix.setdefault(prefix, set()).add(pk)

        excluded = set()
        for prefix, pks in pks_by_prefix.items():
            for pk in flagged[prefix].filter(pk__in=pks).values_list("pk", flat=True):
                excluded.add(line_sources.source_id(prefix, pk))
        return excluded

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
    def _quote_weights(project_file_id):
        """
        ``({quote_id: share of the expediente's net base}, {quote_id: part of
        that quote earning commission})``: the base an advance is taken on
        (accepted quotes plus direct configurations) and, of each quote, what
        is not portes.
        """
        from management.models import ProjectFile
        from invoices.services.project_file_invoice_service import ProjectFileInvoiceService

        project_file = ProjectFile.objects.filter(pk=project_file_id).first()
        if project_file is None:
            return {}, {}
        summary = ProjectFileInvoiceService.get_invoiceable_summary(project_file)
        net_base = summary["total_invoiceable_subtotal_net"]
        if not net_base:
            return {}, {}
        weights = {}
        shares = {}
        for quote_data in summary["quotes"]:
            quote = quote_data["quote"]
            quote_base = (
                Decimal(str(quote.discounted_subtotal.amount))
                if quote.discounted_subtotal else quote_data["subtotal"]
            )
            weights[quote.pk] = quote_base / net_base
            shares[quote.pk] = commission_share(quote)
        return weights, shares

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
