from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("company", "0083_merge_20260917_0923"),
    ]

    operations = [
        migrations.AddField(
            model_name="workingzone",
            name="cnae",
            field=models.CharField(
                blank=True,
                help_text="Optional activity code for this working zone.",
                max_length=10,
                verbose_name="CNAE",
            ),
        ),
        migrations.AddField(
            model_name="workingzone",
            name="nima",
            field=models.CharField(
                blank=True,
                help_text=(
                    "Optional environmental identification number "
                    "(Número de Identificación Medioambiental)."
                ),
                max_length=20,
                verbose_name="NIMA",
            ),
        ),
    ]
