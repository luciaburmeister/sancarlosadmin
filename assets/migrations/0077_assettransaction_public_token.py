import secrets

from django.db import migrations, models


def backfill_public_tokens(apps, schema_editor):
    AssetTransaction = apps.get_model("assets", "AssetTransaction")
    for transaction in AssetTransaction.objects.filter(public_token__isnull=True).iterator():
        transaction.public_token = secrets.token_urlsafe()
        transaction.save(update_fields=["public_token"])


class Migration(migrations.Migration):

    dependencies = [
        ("assets", "0076_add_banera_cuba_size"),
    ]

    operations = [
        migrations.AddField(
            model_name="assettransaction",
            name="public_token",
            field=models.CharField(
                help_text="Unguessable key for the public document link encoded in the QR.",
                max_length=64,
                null=True,
                unique=True,
                verbose_name="Public token",
            ),
        ),
        migrations.RunPython(backfill_public_tokens, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="assettransaction",
            name="public_token",
            field=models.CharField(
                default=secrets.token_urlsafe,
                editable=False,
                help_text="Unguessable key for the public document link encoded in the QR.",
                max_length=64,
                unique=True,
                verbose_name="Public token",
            ),
        ),
    ]
