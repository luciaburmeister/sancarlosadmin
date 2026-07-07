from decimal import Decimal
from django.db import transaction


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

        # Calculate total quantity across all items on the quote
        config_quantity = Decimal(str(
            sum(
                float(i.quantity)
                for config in quote.configurations.all()
                for i in config.items.all()
            )
        ))
        inv_item_quantity = Decimal(str(
            sum(float(i.quantity) for i in quote.inventory_items.all())
        ))
        quote_item_quantity = Decimal(str(
            sum(float(i.quantity) for i in quote.items.all())
        ))
        total_quantity = config_quantity + inv_item_quantity + quote_item_quantity

        subtotal = Decimal(str(quote.subtotal_price.amount))
        discounted_subtotal = Decimal(str(quote.discounted_subtotal.amount)) if quote.discounted_subtotal else subtotal
        total = Decimal(str(quote.total_price.amount))

        for row in rows:
            if not row.agent_id:
                continue

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
            row.save(update_fields=[
                "rule",
                "rule_type_snapshot",
                "rule_value_snapshot",
                "commission_amount",
                "calculated_at",
            ])

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