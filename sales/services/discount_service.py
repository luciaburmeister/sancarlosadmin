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
                # 'accumulated' reflects the running total after prior rules,
                # so it changes as the cascade progresses.
                rule_context = {**context, "accumulated": float(accumulated)}
                try:
                    if not bool(safe_eval(rule.condition, rule_context)):
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
                "kind": rule.kind,
                "percentage": str(rule.percentage),
                "mode": rule.mode,
                "base": str(base.quantize(Decimal("0.01"))),
                "amount": str(amount),
            })

        return total_discount, breakdown

    @staticmethod
    def compute_effective_pct(rules, context: dict) -> tuple:
        """Compute the effective combined discount percentage from ordered rules.

        Runs the standard cascade against a normalised 100-unit base so the
        returned discount amount *is* the effective percentage directly.

        Returns ``(effective_percentage, breakdown)``.
        """
        total, breakdown = DiscountService.compute(Decimal("100"), rules, context)
        return total.quantize(Decimal("0.01")), breakdown
