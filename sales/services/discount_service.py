from decimal import Decimal


class DiscountService:
    """Pure computation: takes a subtotal, ordered rules, and a context dict.
    Returns (total_discount_amount, breakdown_list).
    """

    @staticmethod
    def compute(subtotal: Decimal, rules, context: dict) -> tuple:
        from configurator.utils.evaluator import safe_eval

        total_discount = Decimal("0.00")
        breakdown = []
        accumulated = subtotal  # running total after prior discounts

        for rule in rules:
            # Evaluate condition (empty = always applies)
            if rule.condition and rule.condition.strip():
                try:
                    if not bool(safe_eval(rule.condition, context)):
                        continue
                except Exception:
                    continue  # ponytail: skip broken conditions silently

            # Determine the base for this rule
            if rule.mode == "net":
                base = subtotal
            else:  # accumulated
                base = accumulated

            amount = (base * rule.percentage / Decimal("100")).quantize(Decimal("0.01"))
            total_discount += amount
            accumulated -= amount

            breakdown.append({
                "rule_name": rule.name,
                "percentage": str(rule.percentage),
                "mode": rule.mode,
                "base": str(base.quantize(Decimal("0.01"))),
                "amount": str(amount),
            })

        return total_discount, breakdown
