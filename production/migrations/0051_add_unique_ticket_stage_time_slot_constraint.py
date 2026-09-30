from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("production", "0050_merge_duplicate_ticket_stage_slots"),
    ]

    operations = [
        migrations.AddConstraint(
            model_name="plannedproductionsession",
            constraint=models.UniqueConstraint(
                fields=(
                    "production_ticket",
                    "production_stage",
                    "planned_start",
                    "planned_end",
                ),
                name="unique_ticket_stage_time_slot",
            ),
        ),
    ]
