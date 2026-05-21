from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("materials", "0024_materialfamily_material_family"),
    ]

    operations = [
        migrations.RemoveField(
            model_name="material",
            name="family",
        ),
        migrations.DeleteModel(
            name="MaterialFamily",
        ),
    ]
