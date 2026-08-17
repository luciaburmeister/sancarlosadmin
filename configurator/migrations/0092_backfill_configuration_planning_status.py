from django.db import migrations
from django.utils import timezone


def backfill_planning_status(apps, schema_editor):
    ContentType = apps.get_model("contenttypes", "ContentType")
    Configuration = apps.get_model("configurator", "Configuration")
    ProductionTicket = apps.get_model("production", "ProductionTicket")

    config_ct = ContentType.objects.filter(
        app_label="configurator", model="configuration"
    ).first()
    if config_ct is None:
        return

    planned_config_ids = (
        ProductionTicket.objects.filter(
            content_type=config_ct,
            planned_sessions__isnull=False,
        )
        .exclude(status="cancelled")
        .values_list("object_id", flat=True)
        .distinct()
    )
    Configuration.objects.filter(pk__in=list(planned_config_ids)).update(
        planning_status="planned", planned_at=timezone.now()
    )


def unset_planning_status(apps, schema_editor):
    Configuration = apps.get_model("configurator", "Configuration")
    Configuration.objects.update(planning_status="unplanned", planned_at=None)


class Migration(migrations.Migration):

    dependencies = [
        ("configurator", "0091_configuration_planned_at_and_more"),
        ("production", "0042_productionitem_can_reorder_items_perm"),
        ("contenttypes", "0002_remove_content_type_name"),
    ]

    operations = [
        migrations.RunPython(backfill_planning_status, unset_planning_status),
    ]
