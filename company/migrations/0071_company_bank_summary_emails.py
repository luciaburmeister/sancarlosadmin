import django.contrib.postgres.fields
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('company', '0070_company_require_working_zone_on_project_file'),
    ]

    operations = [
        migrations.AddField(
            model_name='company',
            name='bank_summary_emails',
            field=django.contrib.postgres.fields.ArrayField(base_field=models.EmailField(max_length=254), blank=True, default=list, help_text='Emails that will receive the periodic bank accounts balance summary', size=None, verbose_name='Bank Summary Emails'),
        ),
    ]
