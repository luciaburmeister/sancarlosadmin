from django.db import migrations

ACTIVE_STATUSES = ["draft", "completed", "converted", "shop", "production_done"]


def backfill_production_status(apps, schema_editor):
    Quote = apps.get_model("sales", "Quote")
    Configuration = apps.get_model("configurator", "Configuration")

    with_production = set(
        Configuration.objects.filter(
            quote__isnull=False, status__in=ACTIVE_STATUSES
        ).values_list("quote_id", flat=True)
    )
    not_converted = set(
        Configuration.objects.filter(
            quote__isnull=False, status__in=["draft", "completed"]
        ).values_list("quote_id", flat=True)
    )
    unscheduled = (
        set(
            Configuration.objects.filter(
                quote__isnull=False,
                status__in=ACTIVE_STATUSES,
                planning_status="unplanned",
            ).values_list("quote_id", flat=True)
        )
        - not_converted
    )
    scheduled = with_production - not_converted - unscheduled

    Quote.objects.filter(pk__in=not_converted).update(
        production_status="not_converted"
    )
    Quote.objects.filter(pk__in=unscheduled).update(production_status="unscheduled")
    Quote.objects.filter(pk__in=scheduled).update(production_status="scheduled")


def unset_production_status(apps, schema_editor):
    Quote = apps.get_model("sales", "Quote")
    Quote.objects.update(production_status="no_production")


class Migration(migrations.Migration):

    dependencies = [
        ("sales", "0053_quote_production_status"),
        ("configurator", "0092_backfill_configuration_planning_status"),
    ]

    operations = [
        migrations.RunPython(backfill_production_status, unset_production_status),
    ]
