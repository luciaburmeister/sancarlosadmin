# production/signals.py

from django.db.models.signals import post_save, post_delete
from django.dispatch import receiver
from django.contrib.contenttypes.models import ContentType

from company.models import Employee, Machine
from production.models import PlannedProductionSession, ProductionResource, ProductionTicket
from production.services.planning_status_sync import sync_configuration_planning_status


def sync_production_resource(instance):
    content_type = ContentType.objects.get_for_model(instance.__class__)

    ProductionResource.objects.update_or_create(
        content_type=content_type,
        object_id=instance.pk,
        defaults={
            "company": instance.company,
            "daily_capacity_minutes": getattr(instance, "daily_capacity_minutes", 1440) or 0,
            "parallel_capacity": getattr(instance, "parallel_capacity", 1) or 1,
            "is_active": getattr(instance, "is_active", True),
        },
    )


def delete_production_resource(instance):
    content_type = ContentType.objects.get_for_model(instance.__class__)

    ProductionResource.objects.filter(
        content_type=content_type,
        object_id=instance.pk,
    ).delete()


@receiver(post_save, sender=Employee)
def sync_employee_production_resource(sender, instance, **kwargs):
    sync_production_resource(instance)


@receiver(post_save, sender=Machine)
def sync_machine_production_resource(sender, instance, **kwargs):
    sync_production_resource(instance)


@receiver(post_delete, sender=Employee)
def delete_employee_production_resource(sender, instance, **kwargs):
    delete_production_resource(instance)


@receiver(post_delete, sender=Machine)
def delete_machine_production_resource(sender, instance, **kwargs):
    delete_production_resource(instance)


@receiver(post_save, sender=PlannedProductionSession)
def planned_session_saved(sender, instance, **kwargs):
    if instance.production_ticket_id:
        sync_configuration_planning_status(instance.production_ticket)


@receiver(post_delete, sender=PlannedProductionSession)
def planned_session_deleted(sender, instance, **kwargs):
    if not instance.production_ticket_id:
        return
    try:
        ticket = instance.production_ticket
    except ProductionTicket.DoesNotExist:
        # Cascade delete of the ticket itself; the configuration is being
        # torn down with it, nothing to sync.
        return
    sync_configuration_planning_status(ticket)