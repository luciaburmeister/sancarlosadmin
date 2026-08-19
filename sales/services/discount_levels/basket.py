from sales.models import DiscountLevel
from .base import DiscountLevelHandler


class BasketLevelHandler(DiscountLevelHandler):
    """Applies to the overall quote subtotal (line items + configurations)."""

    level = DiscountLevel.BASKET

    def resolve_base(self, quote, totals):
        return totals["raw_subtotal"]
