from .line_item import LineItemLevelHandler
from .line_items_basket import LineItemsBasketLevelHandler
from .basket import BasketLevelHandler
from .base import PRE_SUBTOTAL, POST_SUBTOTAL


class DiscountLevelRegistry:
    """Explicit registry of DiscountLevel handlers.

    To add a new level, add its handler class's module to this package and
    list the class in ``handler_classes`` below — that's the one place that
    needs to know about it.
    """

    handler_classes = [
        LineItemLevelHandler,
        LineItemsBasketLevelHandler,
        BasketLevelHandler,
    ]

    def __init__(self):
        self.handlers = [cls() for cls in self.handler_classes]

    def pre_subtotal_handlers(self):
        return [h for h in self.handlers if h.phase == PRE_SUBTOTAL]

    def post_subtotal_handlers(self):
        return [h for h in self.handlers if h.phase == POST_SUBTOTAL]
