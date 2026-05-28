from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('sales', '0023_quote_observations'),
    ]

    operations = [
        migrations.AddField(
            model_name='quote',
            name='internal_name',
            field=models.CharField(
                blank=True,
                max_length=200,
                verbose_name='Internal name',
                help_text='Stable name shown in the Fislav app; not affected by PDF renaming.',
            ),
        ),
    ]
