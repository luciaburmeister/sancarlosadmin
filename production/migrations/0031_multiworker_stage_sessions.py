from django.db import migrations, models
from django.db.models import Q


class Migration(migrations.Migration):

    dependencies = [
        ("production", "0030_alter_plannedproductionsession_notes_and_more"),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name="productionitemsession",
            name="unique_open_session_per_employee_item_stage",
        ),
        migrations.AddConstraint(
            model_name="productionitemsession",
            constraint=models.UniqueConstraint(
                fields=["production_item", "production_stage", "created_by"],
                condition=Q(end_time__isnull=True),
                name="unique_open_session_per_employee_item_stage",
            ),
        ),
    ]
