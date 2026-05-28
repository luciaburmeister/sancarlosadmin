from django.db import migrations, models

_PAYMENT_METHOD_CHOICES = [
    ("transfer", "Bank Transfer"),
    ("cash", "Cash"),
    ("card", "Credit/Debit Card"),
    ("cheque", "Cheque"),
    ("sepa_direct_debit", "SEPA - Direct debit"),
    ("confirming", "Confirming"),
]


class Migration(migrations.Migration):

    dependencies = [
        ("invoices", "0051_add_is_external_to_salesinvoice"),
    ]

    operations = [
        migrations.AlterField(
            model_name="invoice",
            name="payment_method",
            field=models.CharField(
                choices=_PAYMENT_METHOD_CHOICES,
                default="transfer",
                max_length=20,
            ),
        ),
        migrations.AlterField(
            model_name="salesinvoice",
            name="payment_method",
            field=models.CharField(
                choices=_PAYMENT_METHOD_CHOICES,
                default="transfer",
                help_text="Expected payment method for this invoice (snapshot at issuance).",
                max_length=20,
                verbose_name="Payment Method",
            ),
        ),
    ]
