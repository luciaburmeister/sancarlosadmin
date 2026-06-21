from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("invoices", "0056_add_reference_to_salesinvoiceitem"),
    ]

    operations = [
        migrations.RenameField(
            model_name="invoice",
            old_name="gross_amount",
            new_name="net_amount",
        ),
        migrations.RenameField(
            model_name="invoice",
            old_name="gross_amount_currency",
            new_name="net_amount_currency",
        ),
    ]
