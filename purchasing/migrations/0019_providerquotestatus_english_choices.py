from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("purchasing", "0018_alter_providerquote_options_and_more"),
    ]

    operations = [
        migrations.AlterField(
            model_name="providerquote",
            name="status",
            field=models.CharField(
                choices=[
                    ("PENDING", "Pending"),
                    ("SENT", "Sent"),
                    ("RESPONDED", "Responded"),
                    ("SELECTED", "Selected"),
                    ("REJECTED", "Rejected"),
                ],
                default="PENDING",
                max_length=20,
                verbose_name="Status",
            ),
        ),
    ]
