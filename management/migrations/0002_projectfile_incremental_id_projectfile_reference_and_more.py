from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('management', '0001_initial'),
    ]

    operations = [
        migrations.AddField(
            model_name='projectfile',
            name='incremental_id',
            field=models.PositiveIntegerField(blank=True, db_index=True, null=True, verbose_name='Incremental ID'),
        ),
        migrations.AddField(
            model_name='projectfile',
            name='reference',
            field=models.CharField(blank=True, max_length=50, null=True, verbose_name='Reference'),
        ),
        migrations.AddConstraint(
            model_name='projectfile',
            constraint=models.UniqueConstraint(fields=['company', 'reference'], name='unique_reference_per_company'),
        ),
    ]
