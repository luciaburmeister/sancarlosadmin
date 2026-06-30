from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("company", "0052_add_bank_account_balance"),
    ]

    operations = [
        migrations.AddField(
            model_name="employee",
            name="work_pin",
            field=models.CharField(
                blank=True,
                help_text="PIN de 4 dígitos hasheado. Se asigna desde el admin.",
                max_length=128,
                null=True,
                verbose_name="PIN de terminal",
            ),
        ),
        migrations.AddField(
            model_name="companyuser",
            name="can_use_shared_terminal",
            field=models.BooleanField(
                default=False,
                help_text="Permite a esta cuenta gestionar sesiones de producción en nombre de cualquier empleado de la empresa.",
                verbose_name="Puede usar terminal compartida",
            ),
        ),
    ]
