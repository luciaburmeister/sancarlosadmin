from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("company", "0034_add_email_log"),
    ]

    operations = [
        migrations.AddField(
            model_name="company",
            name="terms_text",
            field=models.TextField(
                blank=True,
                verbose_name="Términos y Condiciones",
                help_text="Texto que el cliente debe leer y firmar al registrarse. Déjalo vacío para no requerir firma.",
            ),
        ),
    ]
