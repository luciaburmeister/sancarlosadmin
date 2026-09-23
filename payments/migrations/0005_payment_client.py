import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("payments", "0004_alter_payment_amount_currency_and_more"),
        ("sales", "0071_clientaddress_locality"),
    ]

    operations = [
        migrations.AddField(
            model_name="payment",
            name="client",
            field=models.ForeignKey(
                blank=True,
                help_text="The client this money came from (or went back to). Filled in automatically the first time the payment is allocated to one of their sales invoices; set it by hand for a payment on account that has no invoice yet.",
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="payments",
                to="sales.client",
                verbose_name="Client",
            ),
        ),
    ]
