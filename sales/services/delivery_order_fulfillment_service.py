"""Moves a delivery order through its shipping lifecycle.

    Pending shipment -> Prepared -> Dispatched -> Delivered
                (any of the first three) -> Cancelled -> Pending shipment

Every status change goes through here so the admin buttons and anything else
share one set of allowed transitions. The stock itself moves in
``DeliveryOrder.save``: out when the order is dispatched, back when it is
cancelled or taken back to the warehouse.
"""
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext as _

from sales.models import DELIVERY_ORDER_STOCK_OUT_STATUSES, DeliveryOrderStatus
from sales.services.delivery_order_return_service import DeliveryOrderReturnService

S = DeliveryOrderStatus

ALLOWED_TRANSITIONS = {
    S.DRAFT: {S.PREPARED, S.DISPATCHED, S.CANCELLED},
    S.PREPARED: {S.DRAFT, S.DISPATCHED, S.CANCELLED},
    S.DISPATCHED: {S.DELIVERED, S.PREPARED, S.CANCELLED},
    S.DELIVERED: {S.DISPATCHED},
    S.CANCELLED: {S.DRAFT},
}

# Where "undo" takes an order back to.
PREVIOUS_STATUS = {
    S.PREPARED: S.DRAFT,
    S.DISPATCHED: S.PREPARED,
    S.DELIVERED: S.DISPATCHED,
}


class DeliveryOrderFulfillmentService:

    @staticmethod
    def can_transition(delivery_order, target):
        return target in ALLOWED_TRANSITIONS.get(delivery_order.status, set())

    @classmethod
    def _transition(cls, delivery_order, target, **fields):
        if not cls.can_transition(delivery_order, target):
            raise ValidationError(
                _("A delivery order in status “%(current)s” cannot move to “%(target)s”.") % {
                    "current": delivery_order.get_status_display(),
                    "target": S(target).label,
                }
            )
        if (
            delivery_order.is_stock_out
            and target not in DELIVERY_ORDER_STOCK_OUT_STATUSES
            and DeliveryOrderReturnService.has_received_returns(delivery_order)
        ):
            # Leaving these statuses gives every line's stock back; goods a
            # return already brought back would come in twice.
            raise ValidationError(
                _("Part of this delivery order has been returned. Cancel its received returns first.")
            )
        delivery_order.status = target
        for name, value in fields.items():
            setattr(delivery_order, name, value)
        delivery_order.save(update_fields=["status", *fields, "updated_at"])
        return delivery_order

    @classmethod
    def mark_prepared(cls, delivery_order):
        return cls._transition(delivery_order, S.PREPARED)

    @classmethod
    @transaction.atomic
    def dispatch(
        cls,
        delivery_order,
        *,
        dispatched_at=None,
        shipping_method="",
        shipping_agency=None,
        package_count=None,
        shipment_number="",
        tracking_url="",
    ):
        """Record how the order left and take its stock out of the warehouse."""
        return cls._transition(
            delivery_order,
            S.DISPATCHED,
            dispatched_at=dispatched_at or timezone.localdate(),
            shipping_method=shipping_method,
            shipping_agency=shipping_agency,
            package_count=package_count,
            shipment_number=shipment_number,
            tracking_url=tracking_url,
        )

    @classmethod
    def confirm_delivery(cls, delivery_order, *, delivered_at=None, received_by=""):
        return cls._transition(
            delivery_order,
            S.DELIVERED,
            delivered_at=delivered_at or timezone.localdate(),
            received_by=received_by,
        )

    @classmethod
    @transaction.atomic
    def undo(cls, delivery_order):
        """Take the order back one step. Undoing a dispatch returns the stock
        and clears the dispatch date; undoing a delivery clears its record."""
        target = PREVIOUS_STATUS.get(delivery_order.status)
        if target is None:
            raise ValidationError(_("There is nothing to undo on this delivery order."))
        fields = {}
        if delivery_order.status == S.DISPATCHED:
            fields["dispatched_at"] = None
        elif delivery_order.status == S.DELIVERED:
            fields.update(delivered_at=None, received_by="")
        return cls._transition(delivery_order, target, **fields)

    @classmethod
    def cancel(cls, delivery_order):
        return cls._transition(delivery_order, S.CANCELLED)

    @classmethod
    def reopen(cls, delivery_order):
        return cls._transition(delivery_order, S.DRAFT)
