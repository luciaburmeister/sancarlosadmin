from django.db import migrations, models


class Migration(migrations.Migration):
    """Schema only. The backfill lives in 0047 because Postgres refuses
    CREATE INDEX in a transaction that still has pending FK trigger events."""

    dependencies = [
        ("production", "0045_merge_20260914_1316"),
    ]

    operations = [
        migrations.AlterModelOptions(
            name="plannedproductionsession",
            options={
                "permissions": [
                    (
                        "can_reschedule_planned_sessions",
                        "Can reschedule planned production sessions",
                    ),
                ],
                "verbose_name": "Planned Production Session",
                "verbose_name_plural": "Planned Production Sessions",
            },
        ),
        migrations.AddField(
            model_name='plannedproductionsession',
            name='sequence',
            field=models.PositiveSmallIntegerField(default=0, help_text='Position of this stage within its ticket. Stored rather than read from the product template, so editing the template does not silently reorder work that is already planned.', verbose_name='Sequence'),
        ),
        migrations.AddIndex(
            model_name='plannedproductionsession',
            index=models.Index(fields=['company', 'planned_start'], name='production__company_25db62_idx'),
        ),
        migrations.AddIndex(
            model_name='plannedproductionsession',
            index=models.Index(fields=['production_ticket', 'sequence'], name='production__product_ce732f_idx'),
        ),
    ]
