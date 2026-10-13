import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("payments", "0007_paymentallocation_is_mirror"),
        ("materials", "0037_alter_provider_options"),
    ]

    operations = [
        migrations.AddField(
            model_name="payment",
            name="provider",
            field=models.ForeignKey(
                blank=True,
                help_text="The provider this money went to (or came back from). Filled in automatically the first time the payment is allocated to one of their invoices; set it by hand for a payment on account that has no invoice yet.",
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="payments",
                to="materials.provider",
                verbose_name="Provider",
            ),
        ),
    ]
