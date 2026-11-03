"""
A client's discount policies, in the form a sales channel can offer them.

An online store can't run the ERP's discount rules, but it can apply two
kinds of discount: a percentage off every product, and percentage tiers off
the order once its subtotal (after the product discount) reaches a threshold.
``ChannelDiscountService`` translates a client's policies into exactly that,
as the quote would apply them, or says why it can't. Nothing is ever
approximated: a policy the channel would apply differently is not offered.

Channel-neutral: an integration such as ``apps.shopify`` decides how the
result is published and who may claim it.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING

from django.utils.translation import gettext as _

from sales.models import DiscountLevel, DiscountPolicy, DiscountRule
from sales.services.discount_service import DiscountService

if TYPE_CHECKING:
    from company.models import Company
    from sales.models import Client


@dataclass(frozen=True)
class VolumeTier:
    threshold: Decimal  # net subtotal after the product discount, before tax
    percentage: Decimal


@dataclass(frozen=True)
class ChannelDiscount:
    product_percentage: Decimal = Decimal("0")
    tiers: tuple[VolumeTier, ...] = ()
    unsupported: str = ""  # why a channel can't offer it, for staff

    @property
    def is_supported(self) -> bool:
        return not self.unsupported

    @property
    def is_empty(self) -> bool:
        return not self.product_percentage and not self.tiers


class ChannelDiscountService:
    ORDER_LEVELS = (DiscountLevel.LINE_ITEMS_BASKET, DiscountLevel.BASKET)
    # The only condition variable a channel can follow: an order subtotal
    # after the product discount, which is what its tier minimum compares.
    THRESHOLD_VARIABLE = "accumulated"

    @classmethod
    def for_client(cls, company: Company, client: Client) -> ChannelDiscount:
        """What the client's sales get: their own policies, then the company's auto-applied ones."""
        return cls.for_policies(DiscountPolicy.applicable_to(company, client))

    @classmethod
    def for_policies(cls, policies: list[DiscountPolicy]) -> ChannelDiscount:
        rules = DiscountRule.in_precedence(policies)
        product_rules = [rule for rule in rules if rule.level == DiscountLevel.LINE_ITEM]
        order_rules = [rule for rule in rules if rule.level in cls.ORDER_LEVELS]

        for rule in product_rules:
            if rule.condition.strip():
                return cls._unsupported(rule)
        percentage, _breakdown = DiscountService.compute_effective_pct(product_rules, {})
        if not Decimal("0") <= percentage < Decimal("100"):
            return ChannelDiscount(
                unsupported=_("The product discounts add up to %(percentage)s%%, which an online store can't apply.")
                % {"percentage": percentage}
            )

        tiers = []
        for rule in order_rules:
            # Without a condition, an order discount always applies: a tier from zero.
            bounds = cls._bounds(rule.condition) if rule.condition.strip() else (Decimal("0"), None)
            if bounds is None:
                return cls._unsupported(rule)
            tiers.append((bounds, rule))
        if not cls._tiers_exclusive(tiers):
            return ChannelDiscount(
                unsupported=_(
                    "The volume discounts overlap, leave gaps between their thresholds or don't grow with the "
                    "amount, so an online store would apply them differently."
                )
            )
        return ChannelDiscount(
            product_percentage=percentage,
            tiers=tuple(VolumeTier(lower, rule.percentage) for (lower, _upper), rule in tiers),
        )

    @staticmethod
    def _unsupported(rule: DiscountRule) -> ChannelDiscount:
        return ChannelDiscount(
            unsupported=_("%(rule)s has a condition an online store can't apply: %(condition)s")
            % {"rule": rule.name, "condition": rule.condition.strip()}
        )

    @staticmethod
    def _tiers_exclusive(tiers: list[tuple[tuple[Decimal, Decimal | None], DiscountRule]]) -> bool:
        """Whether at most one tier ever applies, as a channel applies only its best one.

        Each tier must end where the next starts and only the last may be
        open-ended; a larger amount never gets a smaller discount.
        """
        tiers.sort(key=lambda tier: tier[0][0])
        if len({rule.level for _limits, rule in tiers}) > 1:
            return False  # each level runs its own cascade, so they stack
        for ((_lower, upper), rule), ((next_lower, _next_upper), next_rule) in zip(tiers, tiers[1:]):
            if upper != next_lower or next_rule.percentage < rule.percentage:
                return False
        return not tiers or tiers[-1][0][1] is None

    @classmethod
    def _bounds(cls, condition: str) -> tuple[Decimal, Decimal | None] | None:
        """``(lower, upper)`` of a condition such as ``accumulated >= 2500 and accumulated <= 5000``.

        ``None`` when it is anything else. Strict and non-strict comparisons
        read the same: they differ only at the exact boundary amount.
        """
        try:
            tree = ast.parse(condition.strip(), mode="eval").body
        except SyntaxError:
            return None
        comparisons = tree.values if isinstance(tree, ast.BoolOp) and isinstance(tree.op, ast.And) else [tree]
        lowers, uppers = [], []
        for node in comparisons:
            if not isinstance(node, ast.Compare):
                return None
            operands = [node.left, *node.comparators]
            for left, operator, right in zip(operands, node.ops, operands[1:]):
                bound = cls._bound(left, operator, right)
                if bound is None:
                    return None
                is_lower, value = bound
                (lowers if is_lower else uppers).append(value)
        if not lowers:
            return None
        return max(lowers), min(uppers) if uppers else None

    @classmethod
    def _bound(cls, left: ast.expr, operator: ast.cmpop, right: ast.expr) -> tuple[bool, Decimal] | None:
        """``(is_lower_bound, value)`` of one ``accumulated <op> number`` comparison, either way round."""
        if cls._is_number(left) and cls._is_variable(right):
            left, right = right, left
            operator = {ast.Gt: ast.Lt(), ast.GtE: ast.LtE(), ast.Lt: ast.Gt(), ast.LtE: ast.GtE()}.get(
                type(operator), operator
            )
        if not (cls._is_variable(left) and cls._is_number(right)):
            return None
        value = Decimal(str(right.value))
        if isinstance(operator, (ast.Gt, ast.GtE)):
            return True, value
        if isinstance(operator, (ast.Lt, ast.LtE)):
            return False, value
        return None

    @classmethod
    def _is_variable(cls, node: ast.expr) -> bool:
        return isinstance(node, ast.Name) and node.id == cls.THRESHOLD_VARIABLE

    @staticmethod
    def _is_number(node: ast.expr) -> bool:
        return isinstance(node, ast.Constant) and type(node.value) in (int, float)
