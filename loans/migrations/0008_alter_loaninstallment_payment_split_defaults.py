import djmoney.models.fields
import djmoney.money
from django.db import migrations
from djmoney.money import Money


def set_zero_defaults(apps, schema_editor):
    LoanInstallment = apps.get_model("loans", "LoanInstallment")
    HistoricalLoanInstallment = apps.get_model("loans", "HistoricalLoanInstallment")

    LoanInstallment.objects.filter(interest_paid__isnull=True).update(
        interest_paid=Money(0, "EUR"),
        interest_paid_currency="EUR",
    )
    LoanInstallment.objects.filter(principal_paid__isnull=True).update(
        principal_paid=Money(0, "EUR"),
        principal_paid_currency="EUR",
    )

    HistoricalLoanInstallment.objects.filter(interest_paid__isnull=True).update(
        interest_paid=Money(0, "EUR"),
        interest_paid_currency="EUR",
    )
    HistoricalLoanInstallment.objects.filter(principal_paid__isnull=True).update(
        principal_paid=Money(0, "EUR"),
        principal_paid_currency="EUR",
    )


class Migration(migrations.Migration):
    dependencies = [
        ("loans", "0007_historicalloaninstallment_interest_paid_and_more"),
    ]

    operations = [
        migrations.RunPython(set_zero_defaults, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="loaninstallment",
            name="interest_paid",
            field=djmoney.models.fields.MoneyField(
                decimal_places=2,
                default=djmoney.money.Money(0, "EUR"),
                help_text="Portion of amount_paid applied to interest. Calculated automatically.",
                max_digits=14,
                verbose_name="Interest Paid",
            ),
        ),
        migrations.AlterField(
            model_name="loaninstallment",
            name="interest_paid_currency",
            field=djmoney.models.fields.CurrencyField(
                choices=[("EUR", "EUR €"), ("GBP", "GBP £"), ("USD", "USD $")],
                default="EUR",
                editable=False,
                max_length=3,
            ),
        ),
        migrations.AlterField(
            model_name="loaninstallment",
            name="principal_paid",
            field=djmoney.models.fields.MoneyField(
                decimal_places=2,
                default=djmoney.money.Money(0, "EUR"),
                help_text="Portion of amount_paid applied to principal. Calculated automatically.",
                max_digits=14,
                verbose_name="Principal Paid",
            ),
        ),
        migrations.AlterField(
            model_name="loaninstallment",
            name="principal_paid_currency",
            field=djmoney.models.fields.CurrencyField(
                choices=[("EUR", "EUR €"), ("GBP", "GBP £"), ("USD", "USD $")],
                default="EUR",
                editable=False,
                max_length=3,
            ),
        ),
        migrations.AlterField(
            model_name="historicalloaninstallment",
            name="interest_paid",
            field=djmoney.models.fields.MoneyField(
                decimal_places=2,
                default=djmoney.money.Money(0, "EUR"),
                help_text="Portion of amount_paid applied to interest. Calculated automatically.",
                max_digits=14,
                verbose_name="Interest Paid",
            ),
        ),
        migrations.AlterField(
            model_name="historicalloaninstallment",
            name="interest_paid_currency",
            field=djmoney.models.fields.CurrencyField(
                choices=[("EUR", "EUR €"), ("GBP", "GBP £"), ("USD", "USD $")],
                default="EUR",
                editable=False,
                max_length=3,
            ),
        ),
        migrations.AlterField(
            model_name="historicalloaninstallment",
            name="principal_paid",
            field=djmoney.models.fields.MoneyField(
                decimal_places=2,
                default=djmoney.money.Money(0, "EUR"),
                help_text="Portion of amount_paid applied to principal. Calculated automatically.",
                max_digits=14,
                verbose_name="Principal Paid",
            ),
        ),
        migrations.AlterField(
            model_name="historicalloaninstallment",
            name="principal_paid_currency",
            field=djmoney.models.fields.CurrencyField(
                choices=[("EUR", "EUR €"), ("GBP", "GBP £"), ("USD", "USD $")],
                default="EUR",
                editable=False,
                max_length=3,
            ),
        ),
    ]
