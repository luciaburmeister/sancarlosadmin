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
        ("sales", "0022_merge_20260525_1635"),
    ]

    operations = [
        migrations.AlterField(
            model_name="client",
            name="payment_method",
            field=models.CharField(
                choices=_PAYMENT_METHOD_CHOICES,
                default="transfer",
                max_length=20,
                verbose_name="Payment Method",
            ),
        ),
    ]
