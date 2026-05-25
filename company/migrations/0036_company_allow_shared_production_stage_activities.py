from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("company", "0035_company_terms_text"),
    ]

    operations = [
        migrations.AddField(
            model_name="company",
            name="allow_shared_production_stage_activities",
            field=models.BooleanField(
                default=True,
                help_text="When enabled, multiple workers can join the same stage while it is in progress. Disable for companies where only one worker may run a stage at a time (e.g. Aluares).",
                verbose_name="Shared production stage activities",
            ),
        ),
    ]
