"""Signals to keep ProjectFile totals in sync.

Triggers recalculation when a Quote, Configuration, SalesInvoice,
Purchase Order, WorkEntry or CostEntry that belongs to a ProjectFile is
saved or deleted.
"""
import logging

from django.db import transaction
from django.db.models.signals import post_save, post_delete, pre_delete
from django.dispatch import receiver

logger = logging.getLogger(__name__)


@receiver(pre_delete, sender="invoices.SalesInvoice")
def sales_invoice_deleting_unlock_work_entries(sender, instance, **kwargs):
    """Unlock work entries that were only invoiced by the invoice being deleted.

    Runs pre_delete because the invoice's items (which carry the
    ``work_entry_<id>`` source ids) cascade away with it.
    """
    from invoices.services.project_file_invoice_service import ProjectFileInvoiceService

    own_source_ids = list(
        instance.items.filter(
            source_id__startswith=ProjectFileInvoiceService.WORK_ENTRY_SOURCE_PREFIX,
        ).values_list("source_id", flat=True)
    )
    ProjectFileInvoiceService.unlock_work_entries(own_source_ids, exclude_invoice=instance)


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
def sales_invoice_saved_update_project_file(sender, instance, update_fields=None, **kwargs):
    """Recalculate ProjectFile totals when a sales invoice is saved."""
    # A payment only moves the payment status, which neither the totals nor
    # the derived status read: leaving the expediente alone keeps payment
    # transactions from locking it after the invoice (invoicing paths lock
    # the expediente first).
    if update_fields is not None and set(update_fields) <= {"payment_status"}:
        return
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
    """A delivery order changing status (→dispatched, →cancelled) or moving to
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


@receiver(pre_delete, sender="invoices.SalesInvoice")
def sales_invoice_deleting_collect_project_files(sender, instance, **kwargs):
    """Note every expediente the invoice counts toward while its M2M rows and
    lines (which carry a bulk invoice's expedientes) still exist."""
    from invoices.services.project_file_invoice_service import ProjectFileInvoiceService

    instance._counted_project_file_ids = ProjectFileInvoiceService.project_file_ids_of(instance)


@receiver(post_delete, sender="invoices.SalesInvoice")
def sales_invoice_deleted_update_project_file(sender, instance, **kwargs):
    """Recalculate the totals of every expediente the deleted invoice counted
    toward (bulk invoices included), and re-derive their status once the
    deletion is committed."""
    from management.models import ProjectFile

    project_file_ids = getattr(instance, "_counted_project_file_ids", None)
    if project_file_ids is None:
        project_file_ids = {instance.project_file_id} - {None}
    # Expedientes deleted in the same operation are simply not found.
    for project_file in ProjectFile.objects.filter(pk__in=project_file_ids):
        try:
            project_file.recalculate_total_amount()
        except Exception:
            logger.debug("ProjectFile %s could not be recalculated.", project_file.pk)
    for project_file_id in project_file_ids:
        transaction.on_commit(
            lambda pk=project_file_id: _recompute_project_file_status(pk)
        )
