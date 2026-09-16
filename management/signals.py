"""Signals to keep ProjectFile totals in sync.

Triggers recalculation when a Quote, Configuration, SalesInvoice,
Purchase Order, WorkEntry or CostEntry that belongs to a ProjectFile is
saved or deleted.
"""
import logging

from django.db.models.signals import post_save, post_delete, pre_delete
from django.dispatch import receiver

logger = logging.getLogger(__name__)


@receiver(pre_delete, sender="invoices.SalesInvoice")
def sales_invoice_deleting_unlock_work_entries(sender, instance, **kwargs):
    """Unlock work entries that were only invoiced by the invoice being deleted.

    Runs pre_delete because the invoice's items (which carry the
    ``work_entry_<id>`` source ids) cascade away with it.
    """
    from invoices.models import SalesInvoiceItem
    from invoices.services.project_file_invoice_service import ProjectFileInvoiceService
    from management.models import WorkEntry

    prefix = ProjectFileInvoiceService.WORK_ENTRY_SOURCE_PREFIX
    own_source_ids = list(
        instance.items.filter(source_id__startswith=prefix).values_list("source_id", flat=True)
    )
    ids = ProjectFileInvoiceService.work_entry_ids_from_source_ids(own_source_ids)
    if not ids:
        return

    still_referenced = set(
        SalesInvoiceItem.objects.filter(
            source_id__in=[f"{prefix}{pk}" for pk in ids],
        ).exclude(invoice=instance).values_list("source_id", flat=True)
    )
    to_unlock = [pk for pk in ids if f"{prefix}{pk}" not in still_referenced]
    if to_unlock:
        WorkEntry.objects.filter(pk__in=to_unlock).update(is_locked=False)


@receiver(post_save, sender="sales.Quote")
def quote_saved_update_project_file(sender, instance, **kwargs):
    """Recalculate ProjectFile total when a quote is saved. Accepting (or
    un-accepting) a quote also changes what the expediente has to deliver."""
    if instance.project_file_id:
        instance.project_file.recalculate_total_amount()
        _recompute_project_file_status(instance.project_file_id)


@receiver(post_delete, sender="sales.Quote")
def quote_deleted_update_project_file(sender, instance, **kwargs):
    """Recalculate ProjectFile total when a quote is deleted."""
    if instance.project_file_id:
        try:
            instance.project_file.recalculate_total_amount()
        except Exception:
            logger.debug("ProjectFile already deleted, skipping recalculation.")
        _recompute_project_file_status(instance.project_file_id)


def _configuration_project_file_id(instance):
    """Expediente a configuration belongs to: directly, or through its quote."""
    if instance.project_file_id:
        return instance.project_file_id
    if instance.quote_id:
        return _quote_project_file_id(instance.quote_id)
    return None


def _quote_project_file_id(quote_id):
    from sales.models import Quote

    return (
        Quote.objects.filter(pk=quote_id)
        .values_list("project_file_id", flat=True)
        .first()
    )


@receiver(post_save, sender="configurator.Configuration")
def configuration_saved_update_project_file(sender, instance, **kwargs):
    """Recalculate ProjectFile total when a direct configuration is saved, and
    re-derive the delivery status: a new or re-quantified configuration is a
    new deliverable."""
    if instance.project_file_id and instance.quote_id is None:
        instance.project_file.recalculate_total_amount()
    _recompute_project_file_status(_configuration_project_file_id(instance))


@receiver(post_delete, sender="configurator.Configuration")
def configuration_deleted_update_project_file(sender, instance, **kwargs):
    """Recalculate ProjectFile total when a direct configuration is deleted."""
    if instance.project_file_id and instance.quote_id is None:
        try:
            instance.project_file.recalculate_total_amount()
        except Exception:
            logger.debug("ProjectFile already deleted, skipping recalculation.")
    _recompute_project_file_status(_configuration_project_file_id(instance))


@receiver(post_save, sender="sales.QuoteInventoryItem")
def quote_inventory_item_saved_update_project_file(sender, instance, **kwargs):
    """Inventory lines on an accepted quote are deliverables too."""
    _recompute_project_file_status(_quote_project_file_id(instance.quote_id))


@receiver(post_delete, sender="sales.QuoteInventoryItem")
def quote_inventory_item_deleted_update_project_file(sender, instance, **kwargs):
    _recompute_project_file_status(_quote_project_file_id(instance.quote_id))


@receiver(post_save, sender="purchasing.PurchaseOrder")
def purchase_order_saved_update_project_file(sender, instance, **kwargs):
    """Recalculate ProjectFile purchased cost when a Purchase Order is saved."""
    if instance.project_file_id:
        instance.project_file.recalculate_total_purchased()


@receiver(post_delete, sender="purchasing.PurchaseOrder")
def purchase_order_deleted_update_project_file(sender, instance, **kwargs):
    """Recalculate ProjectFile purchased cost when a Purchase Order is deleted."""
    if instance.project_file_id:
        try:
            instance.project_file.recalculate_total_purchased()
        except Exception:
            logger.debug("ProjectFile already deleted, skipping recalculation.")


@receiver(post_save, sender="management.WorkEntry")
def work_entry_saved_update_project_file(sender, instance, **kwargs):
    """Recalculate ProjectFile revenue totals when a work entry is saved."""
    if instance.project_file_id:
        instance.project_file.recalculate_total_amount()


@receiver(post_delete, sender="management.WorkEntry")
def work_entry_deleted_update_project_file(sender, instance, **kwargs):
    """Recalculate ProjectFile revenue totals when a work entry is deleted."""
    if instance.project_file_id:
        try:
            instance.project_file.recalculate_total_amount()
        except Exception:
            logger.debug("ProjectFile already deleted, skipping recalculation.")


@receiver(post_save, sender="management.CostEntry")
def cost_entry_saved_update_project_file(sender, instance, **kwargs):
    """Recalculate ProjectFile cost total when a cost entry is saved."""
    if instance.project_file_id:
        instance.project_file.recalculate_total_costs()


@receiver(post_delete, sender="management.CostEntry")
def cost_entry_deleted_update_project_file(sender, instance, **kwargs):
    """Recalculate ProjectFile cost total when a cost entry is deleted."""
    if instance.project_file_id:
        try:
            instance.project_file.recalculate_total_costs()
        except Exception:
            logger.debug("ProjectFile already deleted, skipping recalculation.")


@receiver(post_save, sender="invoices.SalesInvoice")
def sales_invoice_saved_update_project_file(sender, instance, **kwargs):
    """Recalculate ProjectFile totals when a sales invoice is saved."""
    recalculated = set()
    # Single FK
    if instance.project_file_id:
        instance.project_file.recalculate_total_amount()
        recalculated.add(instance.project_file_id)
    # M2M (bulk invoices) — only if saved (has pk)
    if instance.pk:
        for pf in instance.project_files.all():
            if pf.pk not in recalculated:
                pf.recalculate_total_amount()
                recalculated.add(pf.pk)
    # Full invoicing is the last gate before an expediente auto-completes.
    for pf_id in recalculated:
        _recompute_project_file_status(pf_id)


# ── Derived delivery / production status ─────────────────────────────────────

def _recompute_project_file_status(project_file_id):
    from management.services.project_file_status import recompute_project_file_status

    if not project_file_id:
        return
    try:
        recompute_project_file_status(project_file_id)
    except Exception:
        logger.exception("Could not recompute status of ProjectFile %s", project_file_id)


@receiver(post_save, sender="sales.DeliveryOrder")
def delivery_order_saved_update_project_file(sender, instance, **kwargs):
    """A delivery order changing status (draft→sent, →cancelled) or moving to
    another expediente re-derives the delivery status of both sides."""
    _recompute_project_file_status(instance.project_file_id)
    previous = getattr(instance, "_original_project_file_id", None)
    if previous and previous != instance.project_file_id:
        _recompute_project_file_status(previous)


@receiver(post_delete, sender="sales.DeliveryOrder")
def delivery_order_deleted_update_project_file(sender, instance, **kwargs):
    _recompute_project_file_status(instance.project_file_id)


def _delivery_line_project_file_id(instance):
    from sales.models import DeliveryOrder

    return (
        DeliveryOrder.objects.filter(pk=instance.delivery_order_id)
        .values_list("project_file_id", flat=True)
        .first()
    )


@receiver(post_save, sender="sales.DeliveryOrderItem")
def delivery_order_item_saved_update_project_file(sender, instance, **kwargs):
    """Lines added or re-quantified on a sent order change what is delivered."""
    _recompute_project_file_status(_delivery_line_project_file_id(instance))


@receiver(post_delete, sender="sales.DeliveryOrderItem")
def delivery_order_item_deleted_update_project_file(sender, instance, **kwargs):
    _recompute_project_file_status(_delivery_line_project_file_id(instance))


@receiver(post_save, sender="production.ProductionTicket")
def production_ticket_saved_update_project_file(sender, instance, **kwargs):
    """Ticket created, cancelled or re-linked. Status changes driven by items
    and sessions bypass save(), so ProductionTicket.recompute_status() calls
    the recompute itself."""
    pf = instance.resolved_project_file
    _recompute_project_file_status(pf.pk if pf else None)
    previous = getattr(instance, "_original_project_file_id", None)
    if previous and previous != instance.project_file_id:
        _recompute_project_file_status(previous)


@receiver(post_delete, sender="production.ProductionTicket")
def production_ticket_deleted_update_project_file(sender, instance, **kwargs):
    _recompute_project_file_status(instance.project_file_id)


@receiver(post_delete, sender="invoices.SalesInvoice")
def sales_invoice_deleted_update_project_file(sender, instance, **kwargs):
    """Recalculate ProjectFile totals when a sales invoice is deleted."""
    # Note: M2M is cleared on delete so we can only handle the FK
    if instance.project_file_id:
        try:
            instance.project_file.recalculate_total_amount()
        except Exception:
            logger.debug("ProjectFile already deleted, skipping recalculation.")
