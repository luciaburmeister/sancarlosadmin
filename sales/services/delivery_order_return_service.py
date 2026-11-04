"""Goods a client sends back from a delivery order.

    Draft -> Received -> Cancelled
      \\-> Cancelled -> Draft

A draft moves no stock. Receiving the return puts each line's goods back with
a stock-in movement that references the return; cancelling a received return
takes them out again with a compensating movement, so the history stays
auditable. A reason configured not to restock (damaged goods, say) records
the line and counts towards what came back, but moves no stock.

Only lines that actually took stock out can come back (a service ships no
goods), only while the delivery order is dispatched or delivered, and never
more than the line shipped across every received return. Mirrors
``DeliveryOrderItem._deduct_inventory``: a composite item takes its
components back, anything else its own stock.

Identity codes (serial numbers, lots) that came back are picked per return
line from the codes linked to that line or its component lines. They stay
linked while the return is a draft; receiving unlinks them, cancelling a
received return links them back to the line they were shipped on.
"""
from collections import defaultdict
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone
from django.utils.translation import gettext as _

from sales.models import (
    DeliveryOrder,
    DeliveryOrderItem,
    DeliveryOrderReturnCode,
    DeliveryOrderReturnItem,
    DeliveryOrderReturnStatus,
)

S = DeliveryOrderReturnStatus

ALLOWED_TRANSITIONS = {
    S.DRAFT: {S.RECEIVED, S.CANCELLED},
    S.RECEIVED: {S.CANCELLED},
    S.CANCELLED: {S.DRAFT},
}


class DeliveryOrderReturnService:

    # ── What can come back ──────────────────────────────────────────────

    @staticmethod
    def returnable_lines(delivery_order):
        """Lines of ``delivery_order`` whose goods left the warehouse."""
        return delivery_order.items.top_level().filter(stock_deducted=True)

    @staticmethod
    def received_quantities(delivery_order, *, exclude_return=None):
        """``{delivery order line id: units already back}`` over received returns."""
        lines = DeliveryOrderReturnItem.objects.filter(
            delivery_order_return__delivery_order=delivery_order,
            delivery_order_return__status=S.RECEIVED,
        )
        if exclude_return is not None and exclude_return.pk:
            lines = lines.exclude(delivery_order_return=exclude_return)
        return dict(
            lines.values_list("delivery_order_item").annotate(total=Sum("quantity")).order_by()
        )

    @staticmethod
    def shipped_units(line):
        """Units the line took out; stock moves in whole units (see
        ``DeliveryOrderItem._deduct_inventory``)."""
        return int(line.quantity)

    @classmethod
    def check_quantities(cls, delivery_order, requested, *, exclude_return=None):
        """Errors for ``requested`` (``{line: units}``) against what is still
        out; an empty list when everything fits."""
        if not delivery_order.is_stock_out:
            return [_("Only goods of a dispatched or delivered delivery order can be returned.")]
        returnable = set(cls.returnable_lines(delivery_order).values_list("pk", flat=True))
        already = cls.received_quantities(delivery_order, exclude_return=exclude_return)
        errors = []
        for line, units in requested.items():
            if line.pk not in returnable:
                errors.append(
                    _("%(line)s moved no stock, so there is nothing to return.") % {"line": line.item_number}
                )
                continue
            available = cls.shipped_units(line) - already.get(line.pk, 0)
            if units > available:
                errors.append(
                    _("%(line)s: %(requested)s units returned but only %(available)s are still out.") % {
                        "line": line.item_number, "requested": units, "available": max(available, 0),
                    }
                )
        return errors

    # ── Lifecycle ───────────────────────────────────────────────────────

    @staticmethod
    def can_transition(ret, target):
        return target in ALLOWED_TRANSITIONS.get(ret.status, set())

    @classmethod
    def _set_status(cls, ret, target, **fields):
        if not cls.can_transition(ret, target):
            raise ValidationError(
                _("A return in status “%(current)s” cannot move to “%(target)s”.") % {
                    "current": ret.get_status_display(), "target": S(target).label,
                }
            )
        ret.status = target
        for name, value in fields.items():
            setattr(ret, name, value)
        ret.save(update_fields=["status", *fields, "updated_at"])
        return ret

    @classmethod
    @transaction.atomic
    def receive(cls, ret, *, received_at=None, user=None):
        """Put the returned goods back in stock."""
        # One receipt per delivery order at a time, so two returns of the
        # same line cannot both pass the quantity check.
        delivery_order = DeliveryOrder.objects.select_for_update().get(pk=ret.delivery_order_id)
        lines = list(ret.items.select_related(
            "reason", "delivery_order_item__inventory_item__default_warehouse",
        ))
        if not lines:
            raise ValidationError(_("Add at least one line before receiving the return."))

        requested = defaultdict(int)
        for line in lines:
            requested[line.delivery_order_item] += line.quantity
        errors = cls.check_quantities(delivery_order, requested, exclude_return=ret)

        links = cls._locked_code_links(ret)
        moved = [link for link in links if link.identity_code.delivery_order_item_id != link.delivery_order_item_id]
        errors += [
            _("Code %(code)s is no longer linked to %(line)s.") % {
                "code": link.identity_code.code, "line": link.delivery_order_item.item_number,
            }
            for link in moved
        ]
        if not moved:
            codes_by_line = defaultdict(list)
            for link in links:
                codes_by_line[link.return_item_id].append(link.identity_code)
            errors += cls.check_codes([
                (line.delivery_order_item, line.quantity, codes_by_line[line.pk]) for line in lines
            ])
        if errors:
            raise ValidationError(errors)

        cls._set_status(ret, S.RECEIVED, received_at=received_at or timezone.localdate())
        for line in lines:
            cls._move_stock(ret, line, sign=1, user=user)
        for link in links:
            cls._link_code(link.identity_code, None)
        return ret

    @classmethod
    @transaction.atomic
    def cancel(cls, ret, *, user=None):
        """Cancel the return; a received one takes its goods out again and
        links its codes back to the lines they were shipped on."""
        was_received = ret.status == S.RECEIVED
        links = cls._locked_code_links(ret) if was_received else []
        reshipped = [
            link for link in links
            if link.identity_code.delivery_order_item_id not in (None, link.delivery_order_item_id)
        ]
        if reshipped:
            raise ValidationError([
                _("Code %(code)s has been shipped again on %(line)s; unlink it there first.") % {
                    "code": link.identity_code.code,
                    "line": link.identity_code.delivery_order_item.delivery_order.number,
                }
                for link in reshipped
            ])

        cls._set_status(ret, S.CANCELLED)
        if was_received:
            for line in ret.items.select_related("delivery_order_item__inventory_item__default_warehouse"):
                cls._move_stock(ret, line, sign=-1, user=user)
            for link in links:
                cls._link_code(link.identity_code, link.delivery_order_item)
        return ret

    @classmethod
    def reopen(cls, ret):
        return cls._set_status(ret, S.DRAFT, received_at=None)

    @staticmethod
    def has_received_returns(delivery_order):
        return delivery_order.returns.filter(status=S.RECEIVED).exists()

    # ── Identity codes ──────────────────────────────────────────────────

    @staticmethod
    def line_tree(line):
        """``{id: line}`` for ``line`` and its component lines, at any depth."""
        tree, frontier = {line.pk: line}, [line.pk]
        while frontier:
            children = list(DeliveryOrderItem.objects.filter(parent_line_id__in=frontier))
            tree.update((child.pk, child) for child in children)
            frontier = [child.pk for child in children]
        return tree

    @staticmethod
    def code_capacity(top_line, line, units):
        """How many codes ``units`` returned units of ``top_line`` can bring
        back for ``line``: one per unit on the line itself, the BOM share of
        them on a component line."""
        if line.pk == top_line.pk:
            return units
        if not top_line.quantity:
            return 0
        return int(Decimal(units) * line.quantity / top_line.quantity)

    @classmethod
    def check_codes(cls, selections):
        """Errors for ``selections``, ``(returned line, units, codes)`` per
        return line, judged by the line each code is linked to now; an empty
        list when they fit."""
        errors, seen = [], set()
        trees, units, counts = {}, defaultdict(int), defaultdict(int)
        for top, quantity, codes in selections:
            tree = trees.setdefault(top.pk, cls.line_tree(top))
            units[top.pk] += quantity or 0
            for code in codes:
                if code.pk in seen:
                    errors.append(_("Code %(code)s is picked more than once.") % {"code": code.code})
                    continue
                seen.add(code.pk)
                if code.delivery_order_item_id not in tree:
                    errors.append(_("Code %(code)s was not shipped on %(line)s.") % {
                        "code": code.code, "line": top.item_number,
                    })
                    continue
                counts[top.pk, code.delivery_order_item_id] += 1
        for (top_id, line_id), count in counts.items():
            tree = trees[top_id]
            capacity = cls.code_capacity(tree[top_id], tree[line_id], units[top_id])
            if count > capacity:
                errors.append(
                    _("%(line)s: %(count)s codes picked but only %(capacity)s units of it come back.") % {
                        "line": tree[line_id].item_number, "count": count, "capacity": capacity,
                    }
                )
        return errors

    @staticmethod
    def _locked_code_links(ret):
        """The return's code links, each code locked and freshly read."""
        from inventory.models import ItemIdentityCode

        links = list(
            DeliveryOrderReturnCode.objects
            .filter(return_item__delivery_order_return=ret)
            .select_related("delivery_order_item")
            .order_by("pk")
        )
        codes = ItemIdentityCode.objects.select_for_update(of=("self",)).select_related(
            "delivery_order_item__delivery_order",
        ).in_bulk([link.identity_code_id for link in links])
        for link in links:
            link.identity_code = codes[link.identity_code_id]
        return links

    @staticmethod
    def _link_code(code, line):
        """Link ``code`` to ``line``, or unlink it with ``None``; save()
        keeps ``delivered_at`` in step."""
        if code.delivery_order_item_id == (line.pk if line else None):
            return
        code.delivery_order_item = line
        code.save(update_fields=["delivery_order_item", "delivered_at", "updated_at"])

    # ── Stock ───────────────────────────────────────────────────────────

    @staticmethod
    def _move_stock(ret, line, *, sign, user=None):
        """Bring ``line``'s goods back in (``sign=1``) or take them out again
        (``sign=-1``). Idempotent through ``stock_returned``."""
        from inventory.choices import MovementType
        from inventory.models import ItemTransaction

        if line.stock_returned == (sign > 0):
            return  # already where this move would put it
        if sign > 0 and not line.reason.restocks:
            return
        item = line.delivery_order_item.inventory_item
        warehouse = item.default_warehouse  # where dispatch took it from

        do_number = ret.delivery_order.number
        if sign > 0:
            note = f"Returned from Delivery Order {do_number} on {ret.number} ({line.reason})"
        else:
            note = f"Cancelled return {ret.number} of Delivery Order {do_number}"

        if item.is_composite:
            move = item.restock_bom_components if sign > 0 else item.deduct_bom_components
            move(
                quantity=line.quantity,
                warehouse=warehouse,
                reference_object=ret,
                user=user,
                note=note,
            )
        else:
            ItemTransaction.objects.create(
                item=item,
                warehouse=warehouse,
                movement_type=MovementType.IN if sign > 0 else MovementType.OUT,
                quantity_change=sign * line.quantity,
                reference_object=ret,
                user=user,
                note=note,
            )

        line.stock_returned = sign > 0
        line.save(update_fields=["stock_returned", "updated_at"])
