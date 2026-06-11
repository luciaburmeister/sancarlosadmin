"""Signals to keep ProjectFile.total_amount in sync.

Triggers recalculation when a Quote or Configuration that belongs to a
ProjectFile is saved (status or total_price changes) or deleted.
"""
import logging

from django.db.models.signals import post_save, post_delete
from django.dispatch import receiver

logger = logging.getLogger(__name__)


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
