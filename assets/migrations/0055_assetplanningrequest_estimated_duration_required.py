# estimated_duration becomes required on AssetPlanningRequest (and historical).

from datetime import timedelta

from django.db import migrations, models


DEFAULT_DURATION = timedelta(hours=1)


def backfill_null_durations(apps, schema_editor):
    Request = apps.get_model("assets", "AssetPlanningRequest")
    Request.objects.filter(estimated_duration__isnull=True).update(
        estimated_duration=DEFAULT_DURATION,
    )
    Historical = apps.get_model("assets", "HistoricalAssetPlanningRequest")
    Historical.objects.filter(estimated_duration__isnull=True).update(
        estimated_duration=DEFAULT_DURATION,
    )


class Migration(migrations.Migration):

    dependencies = [
        ("assets", "0054_assetplanning_superseded_by"),
    ]

    operations = [
        migrations.RunPython(backfill_null_durations, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="assetplanningrequest",
            name="estimated_duration",
            field=models.DurationField(
                help_text="Expected job length. Used with Needed Time to find a free slot.",
                verbose_name="Estimated Duration",
            ),
        ),
        migrations.AlterField(
            model_name="historicalassetplanningrequest",
            name="estimated_duration",
            field=models.DurationField(
                help_text="Expected job length. Used with Needed Time to find a free slot.",
                verbose_name="Estimated Duration",
            ),
        ),
    ]
