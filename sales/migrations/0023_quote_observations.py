from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('sales', '0022_merge_20260525_1635'),
    ]

    operations = [
        migrations.AddField(
            model_name='quote',
            name='observations',
            field=models.TextField(blank=True, null=True, verbose_name='Observations'),
        ),
    ]
