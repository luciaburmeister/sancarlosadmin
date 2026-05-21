from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("sales", "0015_widen_phone_field"),
    ]

    operations = [
        migrations.AddField(
            model_name="quote",
            name="date",
            field=models.DateField(blank=True, null=True, verbose_name="Date"),
        ),
        migrations.AlterField(
            model_name="quote",
            name="client",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                to="sales.client",
                verbose_name="Client",
            ),
        ),
    ]
