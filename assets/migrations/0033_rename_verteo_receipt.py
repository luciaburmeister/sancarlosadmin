import django.db.models.deletion
from django.db import migrations, models


def rename_verteo_to_receipt(apps, schema_editor):
    AssetTransaction = apps.get_model("assets", "AssetTransaction")
    AssetTransaction.objects.filter(transaction_type="verteo").update(transaction_type="receipt")


class Migration(migrations.Migration):

    dependencies = [
        ('assets', '0031_rename_verteoalbaran_to_assettransactionreceipt'),
    ]

    operations = [
        migrations.RunPython(rename_verteo_to_receipt, migrations.RunPython.noop),
        migrations.RemoveField(
            model_name='assettransactionreceipt',
            name='movement_transaction',
        ),
        migrations.AlterField(
            model_name='assettransactionreceipt',
            name='foto',
            field=models.ImageField(blank=True, null=True, upload_to='transaction_receipts/', verbose_name='Foto del Albaran'),
        ),
        migrations.AlterField(
            model_name='assettransactionreceipt',
            name='transaction',
            field=models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='receipts', to='assets.assettransaction', verbose_name='Transaction'),
        ),
    ]
