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
    """Recalculate ProjectFile total when a quote is saved."""
    if instance.project_file_id:
        instance.project_file.recalculate_total_amount()


@receiver(post_delete, sender="sales.Quote")
def quote_deleted_update_project_file(sender, instance, **kwargs):
    """Recalculate ProjectFile total when a quote is deleted."""
    if instance.project_file_id:
        try:
            instance.project_file.recalculate_total_amount()
        except Exception:
            logger.debug("ProjectFile already deleted, skipping recalculation.")


@receiver(post_save, sender="configurator.Configuration")
def configuration_saved_update_project_file(sender, instance, **kwargs):
    """Recalculate ProjectFile total when a direct configuration is saved."""
    if instance.project_file_id and instance.quote_id is None:
        instance.project_file.recalculate_total_amount()


@receiver(post_delete, sender="configurator.Configuration")
def configuration_deleted_update_project_file(sender, instance, **kwargs):
    """Recalculate ProjectFile total when a direct configuration is deleted."""
    if instance.project_file_id and instance.quote_id is None:
        try:
            instance.project_file.recalculate_total_amount()
        except Exception:
            logger.debug("ProjectFile already deleted, skipping recalculation.")


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


@receiver(post_delete, sender="invoices.SalesInvoice")
def sales_invoice_deleted_update_project_file(sender, instance, **kwargs):
    """Recalculate ProjectFile totals when a sales invoice is deleted."""
    # Note: M2M is cleared on delete so we can only handle the FK
    if instance.project_file_id:
        try:
            instance.project_file.recalculate_total_amount()
        except Exception:
            logger.debug("ProjectFile already deleted, skipping recalculation.")
