import django.contrib.postgres.fields
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("sales", "0069_deliveryorderitem_inventory_item_parent_line"),
    ]

    operations = [
        migrations.AddField(
            model_name="client",
            name="payment_reminder_emails",
            field=django.contrib.postgres.fields.ArrayField(
                base_field=models.EmailField(max_length=254),
                blank=True,
                default=list,
                help_text="Addresses that receive this client's payment reminder emails (e.g. their accounts payable department). Leave empty to send them to the client's main email.",
                size=None,
                verbose_name="Payment Reminder Emails",
            ),
        ),
    ]
