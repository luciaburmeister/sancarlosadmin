from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("company", "0089_workingzone_receipt_notification_email"),
    ]

    operations = [
        migrations.AlterField(
            model_name="machine",
            name="needs_human",
            field=models.BooleanField(default=True, verbose_name="Requires an operator"),
        ),
        migrations.AlterField(
            model_name="machine",
            name="parallel_capacity",
            field=models.PositiveSmallIntegerField(
                default=1,
                help_text="How many items can be processed in parallel",
                verbose_name="Simultaneous jobs",
            ),
        ),
    ]
