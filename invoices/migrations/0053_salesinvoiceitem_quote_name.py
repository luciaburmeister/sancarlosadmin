from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('invoices', '0052_alter_payment_method_choices'),
    ]

    operations = [
        migrations.AddField(
            model_name='salesinvoiceitem',
            name='quote_name',
            field=models.CharField(
                blank=True,
                default='',
                max_length=200,
                verbose_name='Quote name',
                help_text="project_name of the originating quote at invoice creation time; '.' when vehicle is hidden.",
            ),
        ),
    ]
