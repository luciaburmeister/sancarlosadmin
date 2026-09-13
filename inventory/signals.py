from django.contrib.contenttypes.models import ContentType
from django.db.models.signals import post_delete, post_save, pre_save
from django.dispatch import receiver

from inventory.services.stock_position import refresh_stock_position
from management.models import ProjectFile
from purchasing.models import PurchaseOrder, PurchaseOrderItem
from sales.models import (
    DeliveryOrder,
    DeliveryOrderItem,
    Quote,
    QuoteInventoryItem,
)
from .models import InventoryItem, InventoryMovement


@receiver(post_save, sender=InventoryMovement)
def apply_inventory_movement(sender, instance, created, **kwargs):
    if not created:
        return

    item, _ = InventoryItem.objects.get_or_create(
        material=instance.material,
        location=instance.location,
    )

    item.quantity += instance.quantity_change
    item.save()


# ── Cached stock position ────────────────────────────────────────────────────
#
# Every write that can change InventoryItem.incoming_quantity or
# reserved_quantity refreshes the affected items. The handlers only decide
# *which* items to recompute — the figures themselves always come from
# inventory.services.stock_position, which reads them back from source, so a
# handler that fires too often is merely wasteful and one that misses an edge
# leaves a stale row the next refresh repairs.
#
# Each model is watched on the fields the expressions actually read. A pre_save
# snapshot of those fields, taken from the database, lets post_save answer two
# questions exactly: did anything relevant change (skip the refresh if not —
# quote lines in particular are re-saved wholesale by the discount engine), and
# what did it change *from* (so re-pointing a line at another item refreshes
# the item it left as well as the one it joined).

_UNCHANGED = object()  # sentinel: the save could not have touched a watched field


class _Watch:
    """pre_save snapshot of ``fields`` for one model, read back in post_save."""

    def __init__(self, model, fields):
        self.model = model
        self.fields = tuple(fields)
        meta = model._meta
        # update_fields may name a FK by field name or attname; accept both.
        self.names = {
            alias
            for name in self.fields
            for alias in (name, meta.get_field(name).attname)
        }
        self.attnames = tuple(meta.get_field(name).attname for name in self.fields)
        pre_save.connect(self.snapshot, sender=model, weak=False)

    def snapshot(self, sender, instance, update_fields=None, **kwargs):
        if instance._state.adding or instance.pk is None:
            before = None  # a new row: everything is a change
        elif update_fields is not None and not (set(update_fields) & self.names):
            before = _UNCHANGED  # narrow save of unrelated fields
        else:
            before = (
                self.model._default_manager
                .filter(pk=instance.pk)
                .values_list(*self.fields)
                .first()
            )
        instance._stock_position_before = before

    def change(self, instance):
        """``(changed, before)`` for a just-saved instance.

        ``before`` is the snapshot tuple, or None when the row is new.
        """
        before = getattr(instance, "_stock_position_before", None)
        if before is _UNCHANGED:
            return False, None
        if before is None:
            return True, None
        now = tuple(getattr(instance, attname) for attname in self.attnames)
        return now != before, before


# ── Incoming: approved purchase orders not yet received ──────────────────────

_po_lines = _Watch(PurchaseOrderItem, ["inventory_item", "quantity", "received_quantity"])
_purchase_orders = _Watch(PurchaseOrder, ["status"])


@receiver(post_save, sender=PurchaseOrderItem)
def refresh_from_purchase_order_item(sender, instance, **kwargs):
    # Receipts land here too: PurchaseOrderReceiptItem.save()/delete() write
    # po_item.received_quantity through this model's save().
    changed, before = _po_lines.change(instance)
    if not changed:
        return
    previous_item = before[0] if before else None
    refresh_stock_position([previous_item, instance.inventory_item_id])


@receiver(post_delete, sender=PurchaseOrderItem)
def refresh_from_purchase_order_item_delete(sender, instance, **kwargs):
    refresh_stock_position([instance.inventory_item_id])


@receiver(post_save, sender=PurchaseOrder)
def refresh_from_purchase_order(sender, instance, created, **kwargs):
    # Approving, closing or cancelling an order moves every one of its lines
    # into or out of the in-transit figure at once. A brand-new order has no
    # lines yet.
    changed, _ = _purchase_orders.change(instance)
    if created or not changed:
        return
    refresh_stock_position(
        instance.items.values_list("inventory_item_id", flat=True)
    )


# ── Reserved: accepted quotes on open projects, not yet delivered ────────────

_quote_lines = _Watch(QuoteInventoryItem, ["inventory_item", "quantity"])
_quotes = _Watch(Quote, ["status", "project_file"])
_delivery_lines = _Watch(
    DeliveryOrderItem, ["source_content_type", "source_object_id", "quantity"]
)
_delivery_orders = _Watch(DeliveryOrder, ["status"])
_project_files = _Watch(ProjectFile, ["status"])


@receiver(post_save, sender=QuoteInventoryItem)
def refresh_from_quote_line(sender, instance, **kwargs):
    changed, before = _quote_lines.change(instance)
    if not changed:
        return
    previous_item = before[0] if before else None
    refresh_stock_position([previous_item, instance.inventory_item_id])


@receiver(post_delete, sender=QuoteInventoryItem)
def refresh_from_quote_line_delete(sender, instance, **kwargs):
    refresh_stock_position([instance.inventory_item_id])


@receiver(post_save, sender=Quote)
def refresh_from_quote(sender, instance, created, **kwargs):
    # Accepting or un-accepting a quote reserves or releases all of its lines;
    # moving it to another project file can do the same if that project is
    # closed. A brand-new quote has no lines yet.
    changed, _ = _quotes.change(instance)
    if created or not changed:
        return
    refresh_stock_position(
        instance.inventory_items.values_list("inventory_item_id", flat=True)
    )


@receiver(post_save, sender=DeliveryOrderItem)
def refresh_from_delivery_line(sender, instance, **kwargs):
    # Shipping a quoted line releases that part of the reservation.
    changed, before = _delivery_lines.change(instance)
    if not changed:
        return
    ids = list(_quote_line_item_ids(instance.source_content_type_id, [instance.source_object_id]))
    if before:
        ids += _quote_line_item_ids(before[0], [before[1]])
    refresh_stock_position(ids)


@receiver(post_delete, sender=DeliveryOrderItem)
def refresh_from_delivery_line_delete(sender, instance, **kwargs):
    # Deleting the delivery line puts the reservation back.
    refresh_stock_position(
        _quote_line_item_ids(instance.source_content_type_id, [instance.source_object_id])
    )


@receiver(post_save, sender=DeliveryOrder)
def refresh_from_delivery_order(sender, instance, created, **kwargs):
    # Cancelling an order returns its goods, so everything it shipped counts as
    # promised again. A brand-new order has no lines yet.
    changed, _ = _delivery_orders.change(instance)
    if created or not changed:
        return
    quote_line_ct = ContentType.objects.get_for_model(QuoteInventoryItem)
    refresh_stock_position(
        _quote_line_item_ids(
            quote_line_ct.pk,
            instance.items.values_list("source_object_id", flat=True)
            .filter(source_content_type=quote_line_ct),
        )
    )


@receiver(post_save, sender=ProjectFile)
def refresh_from_project_file(sender, instance, created, **kwargs):
    # Completing or cancelling a project releases every reservation its
    # accepted quotes were holding; reopening it takes them back.
    changed, _ = _project_files.change(instance)
    if created or not changed:
        return
    refresh_stock_position(
        QuoteInventoryItem.objects
        .filter(quote__project_file=instance)
        .values_list("inventory_item_id", flat=True)
    )


def _quote_line_item_ids(content_type_id, source_object_ids):
    """Inventory items behind delivery lines sourced from quote lines.

    One query regardless of how many lines. Lines sourced from anything else
    (configurations are produced; materialised discount lines have no goods)
    contribute nothing.
    """
    quote_line_ct = ContentType.objects.get_for_model(QuoteInventoryItem)
    if content_type_id != quote_line_ct.pk:
        return []
    return list(
        QuoteInventoryItem.objects
        .filter(pk__in=source_object_ids)
        .values_list("inventory_item_id", flat=True)
    )
