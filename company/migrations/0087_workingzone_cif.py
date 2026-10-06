from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("company", "0086_company_production_stage_undo_minutes"),
    ]

    operations = [
        migrations.AddField(
            model_name="workingzone",
            name="cif",
            field=models.CharField(
                blank=True,
                help_text="Optional tax identifier of this working zone, printed on the transfer document.",
                max_length=20,
                verbose_name="CIF",
            ),
        ),
    ]
