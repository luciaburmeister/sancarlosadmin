"""
Quote tax calculation strategies.

Two modes controlled by ``company.auto_apply_client_taxes``:

* **ClientTaxStrategy** – taxes come from the client.  The discounted
  subtotal is the base for every tax line.  Simple and predictable.

* **SourceTaxStrategy** – taxes come from each config / inventory item.
  Per-source bases are aggregated and then scaled by the quote-level
  discount factor.
"""

from abc import ABC, abstractmethod
from decimal import Decimal

from django.contrib.contenttypes.models import ContentType

from invoices.models import AppliedTax


# ---------------------------------------------------------------------------
# Abstract strategy
# ---------------------------------------------------------------------------

class TaxStrategy(ABC):
    """Interface every tax strategy must implement."""

    @abstractmethod
    def sync_taxes(self, quote, ct):
        """Ensure the right AppliedTax records exist on *quote*.

        Must return a list of AppliedTax PKs that were kept/created so
        the caller can delete stale ones.
        """

    @abstractmethod
    def build_breakdown(self, quote, ct, raw_subtotal, discounted_subtotal):
        """Return ``(total_tax, breakdown_list)``.

        ``total_tax`` is the additive taxes minus the retentions (IRPF);
        every breakdown entry keeps its positive amount.
        """


# ---------------------------------------------------------------------------
# Client-tax strategy (simple mode)
# ---------------------------------------------------------------------------

class ClientTaxStrategy(TaxStrategy):
    """
    Taxes are defined at the client level and copied to every config.
    The base for each tax is simply the discounted subtotal.
    """

    def sync_taxes(self, quote, ct):
        seen_taxes = {}
        for config in quote.configurations.all():
            for tax in config.get_taxes():
                key = (tax.tax_type_id, tax.selected_percentage)
                if key not in seen_taxes:
                    seen_taxes[key] = {
                        "tax_type": tax.tax_type,
                        "selected_percentage": tax.selected_percentage,
                        "order": tax.order,
                    }
        for inv_item in quote.inventory_items.all():
            for tax in inv_item.get_taxes():
                key = (tax.tax_type_id, tax.selected_percentage)
                if key not in seen_taxes:
                    seen_taxes[key] = {
                        "tax_type": tax.tax_type,
                        "selected_percentage": tax.selected_percentage,
                        "order": tax.order,
                    }

        seen_ids = []
        for info in seen_taxes.values():
            applied, _ = AppliedTax.objects.update_or_create(
                content_type=ct,
                object_id=quote.pk,
                tax_type=info["tax_type"],
                selected_percentage=info["selected_percentage"],
                defaults={"order": info["order"]},
            )
            seen_ids.append(applied.pk)
        return seen_ids

    def build_breakdown(self, quote, ct, raw_subtotal, discounted_subtotal):
        taxes = (
            AppliedTax.objects.filter(content_type=ct, object_id=quote.pk)
            .select_related("tax_type")
            .order_by("order")
        )

        breakdown = []
        total_tax = Decimal("0.00")
        for tax in taxes:
            base = discounted_subtotal
            rate = tax.selected_percentage / Decimal("100")
            calc = (base * rate).quantize(Decimal("0.01"))

            tax.base_amount = base
            tax.calculated_amount = calc
            tax.save(update_fields=["base_amount", "calculated_amount"])

            breakdown.append({
                "tax_name": tax.tax_type.tax_name,
                "percentage": str(tax.selected_percentage),
                "base": str(base.quantize(Decimal("0.01"))),
                "tax_amount": str(calc),
            })
            # Retentions (IRPF) reduce the total, as in SalesInvoice.recalculate_totals
            if tax.tax_type.behavior == "add":
                total_tax += calc
            else:
                total_tax -= calc
        return total_tax, breakdown


# ---------------------------------------------------------------------------
# Source-tax strategy (aggregate mode)
# ---------------------------------------------------------------------------

class SourceTaxStrategy(TaxStrategy):
    """
    Taxes are defined per config / inventory item and may differ.
    Bases are aggregated from each source, then scaled by the
    quote-level discount factor.
    """

    def sync_taxes(self, quote, ct):
        aggregation = {}

        for config in quote.configurations.all():
            for tax in config.get_taxes():
                key = (tax.tax_type_id, tax.selected_percentage)
                if key not in aggregation:
                    aggregation[key] = {
                        "tax_type": tax.tax_type,
                        "selected_percentage": tax.selected_percentage,
                        "base_amount": Decimal("0.00"),
                        "calculated_amount": Decimal("0.00"),
                        "order": tax.order,
                    }
                base = tax.base_amount.amount if tax.base_amount else Decimal("0.00")
                calc = tax.calculated_amount.amount if tax.calculated_amount else Decimal("0.00")
                for tb in config.tax_breakdown or []:
                    if (
                        tb.get("tax_name") == tax.tax_type.tax_name
                        and Decimal(str(tb.get("percentage", 0))) == tax.selected_percentage
                    ):
                        base = Decimal(str(tb.get("base", base)))
                        calc = Decimal(str(tb.get("tax_amount", calc)))
                        break
                aggregation[key]["base_amount"] += base
                aggregation[key]["calculated_amount"] += calc

        for inv_item in quote.inventory_items.all():
            for tax in inv_item.get_taxes():
                key = (tax.tax_type_id, tax.selected_percentage)
                line_base = inv_item.line_total.amount if inv_item.line_total else Decimal("0.00")
                rate = tax.selected_percentage / Decimal("100")
                line_calc = (line_base * rate).quantize(Decimal("0.01"))
                if key not in aggregation:
                    aggregation[key] = {
                        "tax_type": tax.tax_type,
                        "selected_percentage": tax.selected_percentage,
                        "base_amount": Decimal("0.00"),
                        "calculated_amount": Decimal("0.00"),
                        "order": tax.order,
                    }
                aggregation[key]["base_amount"] += line_base
                aggregation[key]["calculated_amount"] += line_calc

        seen_ids = []
        for agg in aggregation.values():
            applied, _ = AppliedTax.objects.update_or_create(
                content_type=ct,
                object_id=quote.pk,
                tax_type=agg["tax_type"],
                selected_percentage=agg["selected_percentage"],
                defaults={
                    "base_amount": agg["base_amount"].quantize(Decimal("0.01")),
                    "calculated_amount": agg["calculated_amount"].quantize(Decimal("0.01")),
                    "order": agg["order"],
                },
            )
            seen_ids.append(applied.pk)
        return seen_ids

    def build_breakdown(self, quote, ct, raw_subtotal, discounted_subtotal):
        taxes = (
            AppliedTax.objects.filter(content_type=ct, object_id=quote.pk)
            .select_related("tax_type")
            .order_by("order")
        )

        if raw_subtotal > 0:
            discount_factor = discounted_subtotal / raw_subtotal
        else:
            discount_factor = Decimal("1")

        breakdown = []
        total_tax = Decimal("0.00")
        for tax in taxes:
            orig_base = tax.base_amount.amount if tax.base_amount else Decimal("0.00")
            base = (orig_base * discount_factor).quantize(Decimal("0.01"))

            rate = tax.selected_percentage / Decimal("100")
            calc = (base * rate).quantize(Decimal("0.01"))

            tax.base_amount = base
            tax.calculated_amount = calc
            tax.save(update_fields=["base_amount", "calculated_amount"])

            breakdown.append({
                "tax_name": tax.tax_type.tax_name,
                "percentage": str(tax.selected_percentage),
                "base": str(base),
                "tax_amount": str(calc),
            })
            # Retentions (IRPF) reduce the total, as in SalesInvoice.recalculate_totals
            if tax.tax_type.behavior == "add":
                total_tax += calc
            else:
                total_tax -= calc
        return total_tax, breakdown


# ---------------------------------------------------------------------------
# Service (picks the right strategy)
# ---------------------------------------------------------------------------

class QuoteTaxService:
    """Applies the correct tax strategy for a quote."""

    @staticmethod
    def get_strategy(quote) -> TaxStrategy:
        if quote.company_id and quote.company.auto_apply_client_taxes:
            return ClientTaxStrategy()
        return SourceTaxStrategy()

    @classmethod
    def apply(cls, quote, raw_subtotal, discounted_subtotal):
        """Sync taxes and build breakdown. Returns ``(tax_amount, breakdown)``."""
        strategy = cls.get_strategy(quote)
        ct = ContentType.objects.get_for_model(quote)

        seen_ids = strategy.sync_taxes(quote, ct)

        # Remove stale taxes
        AppliedTax.objects.filter(
            content_type=ct, object_id=quote.pk,
        ).exclude(pk__in=seen_ids).delete()

        return strategy.build_breakdown(quote, ct, raw_subtotal, discounted_subtotal)
