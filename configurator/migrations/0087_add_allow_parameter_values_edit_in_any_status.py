from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("configurator", "0086_alter_configuration_price_override_currency_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="producttemplate",
            name="allow_parameter_values_edit_in_any_status",
            field=models.BooleanField(
                default=False,
                help_text="If enabled, parameter values can be edited even when the configuration is in production, completed, or any non-draft status.",
                verbose_name="Allow changing parameter values in any status",
            ),
        ),
    ]
