from .line_item import LineItemPassThroughStrategy
from .financing import FinancingDiscountStrategy
from .line_items_basket import LineItemsBasketDiscountStrategy
from .basket import BasketDiscountStrategy


class InvoiceDiscountStrategyRegistry:
    """Explicit registry of invoice discount strategies.

    To add a new behaviour, add its strategy class's module to this package
    and list the class below — that's the one place that needs to know about
    it. Order matters: the first strategy whose ``matches`` returns True
    claims the breakdown entry, so keep the most specific ones (kind-based)
    before the level-based fallbacks.
    """

    strategy_classes = [
        LineItemPassThroughStrategy,
        FinancingDiscountStrategy,
        LineItemsBasketDiscountStrategy,
        BasketDiscountStrategy,
    ]

    def __init__(self):
        self.strategies = [cls() for cls in self.strategy_classes]

    def strategy_for(self, entry):
        for strategy in self.strategies:
            if strategy.matches(entry):
                return strategy
        return None
