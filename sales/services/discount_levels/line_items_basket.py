from sales.models import DiscountLevel
from .base import DiscountLevelHandler


class LineItemsBasketLevelHandler(DiscountLevelHandler):
    """Applies only to the line items subtotal, excluding configurations."""

    level = DiscountLevel.LINE_ITEMS_BASKET

    def resolve_base(self, quote, totals):
        return totals["item_total"]
