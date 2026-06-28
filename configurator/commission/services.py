from decimal import Decimal
from django.db import transaction


class CommissionService:

    @classmethod
    @transaction.atomic
    def prepopulate_for_quote(cls, quote) -> None:
        """
        Creates one QuoteCommission row per configuration on the quote,
        if one does not already exist.
        """
        from configurator.commission.models import QuoteCommission

        existing_config_ids = set(
            QuoteCommission.objects.filter(
                quote=quote
            ).values_list("configuration_id", flat=True)
        )

        for config in quote.configurations.all():
            if config.pk not in existing_config_ids:
                QuoteCommission.objects.create(
                    quote=quote,
                    configuration=config,
                    agent=None,
                )

    @classmethod
    @transaction.atomic
    def recalculate(cls, quote) -> None:
        """
        Recalculates commission_amount for every QuoteCommission row
        belonging to this quote.
        If no AgentCommissionRule exists for the employee + product template,
        the commission is set to zero — no override or auto-creation.
        """
        from configurator.commission.models import AgentCommissionRule, QuoteCommission

        rows = list(
            QuoteCommission.objects.filter(
                quote=quote
            ).select_related("agent", "rule", "configuration__product")
        )

        for row in rows:
            if not row.agent_id:
                continue

            config = row.configuration
            subtotal = Decimal(str(config.subtotal_price.amount))
            total = Decimal(str(config.total_price.amount))
            quantity = Decimal(str(
                sum(float(i.quantity) for i in config.items.all())
            ))
            product_template = config.product

            try:
                rule = AgentCommissionRule.objects.get(
                    agent_id=row.agent_id,
                    product_template=product_template,
                    is_active=True,
                )
                row.rule = rule
                row.rule_type_snapshot = rule.rule_type
                row.rule_value_snapshot = rule.value
                amount = rule.compute_commission(subtotal, total, quantity)

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
            .select_related("agent", "rule", "configuration__product")
            .order_by("configuration_id")
        )

        result = []
        for row in rows:
            if not row.agent_id:
                continue
            result.append({
                "agent_name": row.agent.name,
                "configuration_name": str(row.configuration),
                "product_template_name": row.configuration.product.name,
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