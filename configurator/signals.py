from django.db.models.signals import post_delete, post_save, pre_save
from django.dispatch import receiver

from configurator.models import Configuration
from sales.services.quote_production_status import recompute_quote_production_status


@receiver(pre_save, sender=Configuration)
def stash_previous_quote(sender, instance, **kwargs):
    # Remember the quote the configuration pointed at before this save, so a
    # reassignment also refreshes the quote that lost the configuration.
    if instance.pk:
        instance._previous_quote_id = (
            Configuration.objects.filter(pk=instance.pk)
            .values_list("quote_id", flat=True)
            .first()
        )
    else:
        instance._previous_quote_id = None


@receiver(post_save, sender=Configuration)
def sync_quote_production_status_on_save(sender, instance, **kwargs):
    previous_quote_id = getattr(instance, "_previous_quote_id", None)
    if previous_quote_id and previous_quote_id != instance.quote_id:
        recompute_quote_production_status(previous_quote_id)
    recompute_quote_production_status(instance.quote_id)


@receiver(post_delete, sender=Configuration)
def sync_quote_production_status_on_delete(sender, instance, **kwargs):
    recompute_quote_production_status(instance.quote_id)
