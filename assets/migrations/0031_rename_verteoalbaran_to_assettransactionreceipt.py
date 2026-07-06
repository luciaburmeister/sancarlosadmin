from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("assets", "0030_alter_assettransaction_transaction_type_and_more"),
    ]

    operations = [
        migrations.RenameModel(
            old_name="VerteoAlbaran",
            new_name="AssetTransactionReceipt",
        ),
        migrations.RenameField(
            model_name="assettransactionreceipt",
            old_name="verteo_transaction",
            new_name="transaction",
        ),
        migrations.RenameField(
            model_name="assettransactionreceipt",
            old_name="foto_albaran",
            new_name="foto",
        ),
        migrations.AlterModelOptions(
            name="assettransactionreceipt",
            options={
                "ordering": ["-created_at"],
                "verbose_name": "Asset Transaction Receipt",
                "verbose_name_plural": "Asset Transaction Receipts",
            },
        ),
    ]
