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
        ("payments", "0001_initial"),
    ]

    operations = [
        migrations.AlterField(
            model_name="payment",
            name="method",
            field=models.CharField(
                choices=_PAYMENT_METHOD_CHOICES,
                max_length=20,
                verbose_name="Method",
            ),
        ),
    ]
