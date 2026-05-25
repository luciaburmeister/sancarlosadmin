from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("invoices", "0048_add_source_id_to_salesinvoiceitem"),
    ]

    operations = [
        migrations.AddField(
            model_name="salesinvoice",
            name="payment_method",
            field=models.CharField(
                choices=[
                    ("transfer", "Bank Transfer"),
                    ("cash", "Cash"),
                    ("card", "Credit/Debit Card"),
                ],
                default="transfer",
                help_text="Expected payment method for this invoice (snapshot at issuance).",
                max_length=20,
                verbose_name="Payment Method",
            ),
        ),
    ]
