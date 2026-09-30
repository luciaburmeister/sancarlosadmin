from django.contrib.postgres.fields import ArrayField
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("production", "0048_merge_per_resource_sessions"),
    ]

    operations = [
        migrations.AddField(
            model_name="productionticket",
            name="stage_order",
            field=ArrayField(
                base_field=models.CharField(max_length=50),
                blank=True,
                default=list,
                help_text="Ticket-specific stage order. Empty uses the product's default order.",
                verbose_name="Production Stage Order",
            ),
        ),
    ]
