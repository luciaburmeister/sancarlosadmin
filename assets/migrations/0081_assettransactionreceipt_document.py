import secrets

from django.db import migrations, models


def backfill_public_tokens(apps, schema_editor):
    AssetTransactionReceipt = apps.get_model("assets", "AssetTransactionReceipt")
    for receipt in AssetTransactionReceipt.objects.filter(public_token__isnull=True).iterator():
        receipt.public_token = secrets.token_urlsafe()
        receipt.save(update_fields=["public_token"])


class Migration(migrations.Migration):

    dependencies = [
        ('assets', '0080_alter_assettransactionreceipt_options_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='assettransactionreceipt',
            name='updated_at',
            field=models.DateTimeField(auto_now=True, verbose_name='Updated At'),
        ),
        migrations.AddField(
            model_name='assettransactionreceipt',
            name='document_file',
            field=models.FileField(blank=True, editable=False, upload_to='asset_transaction_receipts/documents/', verbose_name='Document'),
        ),
        migrations.AddField(
            model_name='assettransactionreceipt',
            name='document_generated_at',
            field=models.DateTimeField(blank=True, editable=False, null=True, verbose_name='Document generated at'),
        ),
        migrations.AddField(
            model_name='assettransactionreceipt',
            name='public_token',
            field=models.CharField(
                help_text='Unguessable key for the public document link encoded in the QR.',
                max_length=64,
                null=True,
                unique=True,
                verbose_name='Public token',
            ),
        ),
        migrations.RunPython(backfill_public_tokens, migrations.RunPython.noop),
        migrations.AlterField(
            model_name='assettransactionreceipt',
            name='public_token',
            field=models.CharField(
                default=secrets.token_urlsafe,
                editable=False,
                help_text='Unguessable key for the public document link encoded in the QR.',
                max_length=64,
                unique=True,
                verbose_name='Public token',
            ),
        ),
    ]
