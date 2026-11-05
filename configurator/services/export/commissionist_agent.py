from collections import defaultdict
from datetime import timedelta
from decimal import Decimal
from typing import Any, Dict

from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from .base import BaseExportStrategy
from .context_helpers import get_company_context

ZERO = Decimal("0.00")


class SettlementScope(models.TextChoices):
    """Which commission an agent report's settlement lists."""

    PERIOD = "period", _("Released in the period")
    PENDING = "pending", _("Pending settlement up to the end date")
    RECORDED = "recorded", _("A recorded settlement")


class CommissionistAgentExportStrategy(BaseExportStrategy):
    """
    Strategy for an agent's monthly reports (agent_report templates): the
    quotes, invoices and commission settlement of one agent in one period.
    Defaults to the last closed month, which is what template previews show.
    The settlement lists what was released in the period, what is pending
    settlement up to its end, or the lines of a recorded ``settlement``.
    """

    def __init__(
        self, agent, date_from=None, date_to=None, settlement_scope=SettlementScope.PERIOD, settlement=None,
    ):
        super().__init__(agent)
        self.agent = agent
        if date_from is None or date_to is None:
            date_to = timezone.localdate().replace(day=1) - timedelta(days=1)
            date_from = date_to.replace(day=1)
        self.date_from = date_from
        self.date_to = date_to
        self.settlement = settlement
        if settlement is not None:
            settlement_scope = SettlementScope.RECORDED
        self.settlement_scope = SettlementScope(settlement_scope)

    def get_product(self):
        return None

    def get_context(self, include_rendered_templates: bool = True) -> Dict[str, Any]:
        return {
            **get_company_context(self.agent.company),
            "agent": self.agent,
            "date_from": self.date_from,
            "date_to": self.date_to,
            "printed_on": timezone.localdate(),
            "quotes": self.get_quotes(),
            "invoices": self.get_invoices(),
            "settlement": self.get_settlement(),
        }

    @staticmethod
    def column_totals(rows, keys):
        return {key: sum((row[key] for row in rows), ZERO) for key in keys}

    def get_quotes(self):
        """Quotes issued to the agent's clients in the period, drafts excluded."""
        from invoices.models import TaxType
        from sales.models import Quote, QuoteStatus

        quotes = (
            Quote.objects.for_agent(self.agent)
            .filter(date__range=(self.date_from, self.date_to))
            .exclude(status=QuoteStatus.DRAFT)
            .select_related("client")
            .order_by("date", "pk")
        )
        # The stored breakdown names each tax; retentions (IRPF) are the
        # subtracting tax types and are netted out of quote.tax_amount.
        retention_names = set(TaxType.objects.filter(behavior="subtract").values_list("tax_name", flat=True))
        rows = []
        for quote in quotes:
            retention = sum(
                (Decimal(tax["tax_amount"]) for tax in quote.tax_breakdown or []
                 if tax.get("tax_name") in retention_names),
                ZERO,
            )
            rows.append({
                "number": quote.pk,
                "name": quote.project_name,
                "date": quote.date,
                "status": quote.get_status_display(),
                "client_code": quote.client_id,
                "client_name": quote.client.name if quote.client_id else "",
                "payment_method": quote.get_payment_method_display(),
                "base": quote.discounted_subtotal.amount,
                "retention": retention,
                "taxes": quote.tax_amount.amount + retention,
                "total": quote.total_price.amount,
            })
        return {"rows": rows, "totals": self.column_totals(rows, ("base", "retention", "taxes", "total"))}

    def get_invoices(self):
        """Final invoices of the period the agent earns commission on, with their lines and a VAT summary."""
        from django.contrib.contenttypes.models import ContentType
        from django.db.models import Prefetch
        from django.db.models.functions import Length

        from invoices.models import AppliedTax, SalesInvoice, SalesInvoiceItem, SalesInvoiceStatus

        # The agent's invoices are the ones in the commissions list.
        invoices = list(
            SalesInvoice.objects
            .filter(
                company_id=self.agent.company_id,
                commissions__agent=self.agent,
                status=SalesInvoiceStatus.FINAL,
                date_issued__range=(self.date_from, self.date_to),
            )
            .select_related("client")
            .prefetch_related(Prefetch("items", queryset=SalesInvoiceItem.objects.order_by("pk")))
            # Shorter numbers first, so 26FC-98 sorts before 26FC-100.
            .order_by("date_issued", Length("invoice_number"), "invoice_number")
        )
        taxes = defaultdict(list)
        applied = (
            AppliedTax.objects
            .filter(
                content_type=ContentType.objects.get_for_model(SalesInvoice),
                object_id__in=[invoice.pk for invoice in invoices],
            )
            .select_related("tax_type")
            .order_by("order", "pk")
        )
        for tax in applied:
            taxes[tax.object_id].append(tax)

        rows = []
        vat_groups = {}
        for invoice in invoices:
            base = invoice.taxable_base.amount
            retention = ZERO
            for tax in taxes[invoice.pk]:
                amount = tax.calculated_amount.amount if tax.calculated_amount else ZERO
                if tax.tax_type.behavior == "subtract":
                    retention += amount
                    continue
                group = vat_groups.setdefault(
                    (tax.tax_type.tax_name, tax.selected_percentage),
                    {"name": tax.tax_type.tax_name, "percentage": tax.selected_percentage,
                     "base": ZERO, "amount": ZERO, "invoice_ids": set()},
                )
                group["base"] += tax.base_amount.amount if tax.base_amount else base
                group["amount"] += amount
                group["invoice_ids"].add(invoice.pk)
            rows.append({
                "number": invoice.invoice_number or invoice.proforma_number or "",
                "date": invoice.date_issued,
                "client_code": invoice.client_id,
                "client_name": invoice.client.name,
                "payment_method": invoice.get_payment_method_display(),
                "base": base,
                "retention": retention,
                "taxes": invoice.tax_amount.amount + retention,
                "total": invoice.total_amount.amount,
                "items": [
                    {
                        "reference": item.reference,
                        "description": item.description,
                        "quantity": item.quantity,
                        "unit_price": item.unit_price.amount,
                        "discount_percentage": item.discount_percentage,
                        "line_total": item.line_total.amount,
                    }
                    for item in invoice.items.all()
                ],
            })
        vat_summary = [
            {
                "name": group["name"],
                "percentage": group["percentage"],
                "base": group["base"],
                "amount": group["amount"],
                "count": len(group["invoice_ids"]),
            }
            for group in sorted(vat_groups.values(), key=lambda group: (group["name"], group["percentage"]))
        ]
        return {
            "rows": rows,
            "totals": self.column_totals(rows, ("base", "retention", "taxes", "total")),
            "vat_summary": vat_summary,
            "vat_totals": self.column_totals(vat_summary, ("base", "amount")),
        }

    def get_settlement(self):
        """
        The settlement's rows and totals, of the chosen scope; ``number`` is
        the recorded settlement's, ``cut_off`` the date it settles up to.
        """
        entries = {
            SettlementScope.PERIOD: self.released_in_period,
            SettlementScope.PENDING: self.pending_settlement,
            SettlementScope.RECORDED: self.recorded_settlement,
        }[self.settlement_scope]()
        rows = [self.settlement_row(*entry) for entry in entries]
        totals = self.column_totals(rows, ("base", "commission"))
        totals["percentage"] = (totals["commission"] / totals["base"] * 100).quantize(ZERO) if totals["base"] else ZERO
        return {
            "rows": rows,
            "totals": totals,
            "scope": self.settlement_scope.value,
            "number": self.settlement.pk if self.settlement is not None else None,
            "cut_off": self.settlement.settled_until if self.settlement is not None else self.date_to,
        }

    @staticmethod
    def settlement_row(invoice, base, commission, rate, paid_on):
        """One row of the settlement; ``invoice`` comes with its client and delivery orders."""
        from configurator.commission.admin import commission_document_label

        if rate is None:
            rate = (commission / base * 100).quantize(ZERO) if base else ZERO
        return {
            "date": invoice.date_issued,
            "document": commission_document_label(invoice),
            "delivery_orders": ", ".join(order.number for order in invoice.delivery_orders.all()),
            "client_code": invoice.client_id,
            "client_name": invoice.client.name,
            "paid_on": paid_on,
            "base": base,
            "percentage": rate,
            "commission": commission,
        }

    def pending_settlement(self):
        """Commission released up to the end of the period that no settlement has paid yet."""
        from configurator.commission.services import CommissionSettlementService

        for invoice in CommissionSettlementService.pending_invoices(self.agent, self.date_to):
            yield (
                invoice, invoice.pending_base, invoice.pending_commission,
                invoice.commission_rate, invoice.released_on,
            )

    def recorded_settlement(self):
        """The lines of the recorded settlement still settled, as they were settled."""
        from django.db.models.functions import Length

        lines = (
            self.settlement.lines.uncancelled()
            .select_related("sales_invoice__client")
            .prefetch_related("sales_invoice__delivery_orders")
            .order_by(
                "sales_invoice__date_issued",
                Length("sales_invoice__invoice_number"),
                "sales_invoice__invoice_number",
            )
        )
        for line in lines:
            yield (
                line.sales_invoice, line.base_amount.amount, line.commission_amount.amount,
                line.commission_rate, line.released_on,
            )

    def released_in_period(self):
        """
        Commission released in the period by the agent's collected invoices,
        whenever they were issued: an invoice paid in parts appears in each
        period that released part of its commission.
        """
        from django.db.models import Max, Sum
        from django.db.models.functions import Length

        from configurator.commission.models import CommissionRelease, InvoiceCommission

        released = {
            row["invoice_commission"]: row
            for row in (
                CommissionRelease.objects
                .filter(
                    agent=self.agent,
                    sales_invoice__company_id=self.agent.company_id,
                    release_date__range=(self.date_from, self.date_to),
                )
                .values("invoice_commission")
                .annotate(base=Sum("base_amount"), commission=Sum("released_amount"), paid_on=Max("release_date"))
            )
        }
        commissions = (
            InvoiceCommission.objects
            .filter(pk__in=released)
            .select_related("sales_invoice__client")
            .prefetch_related("sales_invoice__delivery_orders")
            .order_by(
                "sales_invoice__date_issued",
                Length("sales_invoice__invoice_number"),
                "sales_invoice__invoice_number",
            )
        )
        for commission in commissions:
            period = released[commission.pk]
            yield (
                commission.sales_invoice, period["base"] or ZERO, period["commission"] or ZERO,
                commission.commission_rate, period["paid_on"],
            )
