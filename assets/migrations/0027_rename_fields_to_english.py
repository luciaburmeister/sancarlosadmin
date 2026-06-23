from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('assets', '0026_rename_planning_type_and_load_description'),
    ]

    operations = [
        migrations.RenameField(
            model_name='assetplanning',
            old_name='tipo_planificacion',
            new_name='planning_type',
        ),
        migrations.RenameField(
            model_name='assetplanning',
            old_name='descripcion_carga',
            new_name='load_description',
        ),
        migrations.RenameField(
            model_name='historicalassetplanning',
            old_name='tipo_planificacion',
            new_name='planning_type',
        ),
        migrations.RenameField(
            model_name='historicalassetplanning',
            old_name='descripcion_carga',
            new_name='load_description',
        ),
    ]
