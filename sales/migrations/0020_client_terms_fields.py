from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("sales", "0019_quote_photo"),
    ]

    operations = [
        migrations.AddField(
            model_name="client",
            name="terms_accepted_at",
            field=models.DateTimeField(blank=True, null=True, verbose_name="T&C Accepted At"),
        ),
        migrations.AddField(
            model_name="client",
            name="terms_signature",
            field=models.TextField(
                blank=True,
                verbose_name="Firma (base64)",
                help_text="PNG de la firma del cliente codificado en base64",
            ),
        ),
        migrations.AddField(
            model_name="client",
            name="terms_version",
            field=models.CharField(
                blank=True,
                max_length=64,
                verbose_name="Versión T&C",
                help_text="Hash SHA-256 (primeros 8 chars) del texto de T&C firmado",
            ),
        ),
    ]
