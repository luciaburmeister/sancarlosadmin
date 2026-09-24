from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("sales", "0071_clientaddress_locality"),
    ]

    operations = [
        migrations.AddField(
            model_name="client",
            name="nima",
            field=models.CharField(
                blank=True,
                help_text="Optional environmental identification number (Número de Identificación Medioambiental).",
                max_length=20,
                verbose_name="NIMA",
            ),
        ),
    ]
