from django.db import migrations
import django.db.models.deletion
import djmoney.models.fields


class Migration(migrations.Migration):

    dependencies = [
        ("company", "0051_copy_token_data"),
    ]

    operations = [
        migrations.AddField(
            model_name="bankaccount",
            name="last_known_balance_currency",
            field=djmoney.models.fields.CurrencyField(
                choices=[("EUR", "Euro")],
                default="EUR",
                editable=False,
                max_length=3,
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="bankaccount",
            name="last_known_balance",
            field=djmoney.models.fields.MoneyField(
                blank=True,
                decimal_places=2,
                default=None,
                help_text="Balance reported by the bank on the last sync.",
                max_digits=14,
                null=True,
                verbose_name="Last Known Balance",
            ),
        ),
        migrations.AddField(
            model_name="bankaccount",
            name="last_balance_updated_at",
            field=django.db.models.fields.DateTimeField(
                blank=True,
                help_text="When the balance was last refreshed from the bank.",
                null=True,
                verbose_name="Balance Updated At",
            ),
        ),
    ]
