from django.db import migrations, models


def backfill_pause_dates(apps, schema_editor):
    """Every AssetTransactionPause created before this field existed (see
    0088) defaults to its movement's own date, same as a brand new one gets
    in AssetTransactionPause.save(). F("movement__date") can't be used
    directly in update() -- it's a joined field -- so this loops instead."""
    AssetTransactionPause = apps.get_model("assets", "AssetTransactionPause")

    pauses = list(AssetTransactionPause.objects.filter(date__isnull=True).select_related("movement"))
    for pause in pauses:
        pause.date = pause.movement.date
    AssetTransactionPause.objects.bulk_update(pauses, ["date"])


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        # Deliberately before 0088, not after -- see the comment on 0088's
        # own dependencies list.
        ('assets', '0087_add_transaction_pause'),
    ]

    operations = [
        migrations.AddField(
            model_name='assettransactionpause',
            name='date',
            field=models.DateTimeField(blank=True, help_text="Date printed on this pause's document. Defaults to the movement's own date; hand-editable by a superuser only, to correct it.", null=True, verbose_name='Date'),
        ),
        migrations.RunPython(backfill_pause_dates, noop),
    ]
