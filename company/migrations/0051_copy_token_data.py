"""Copy nuek_account_id values to the new nuek_token field."""

from django.db import migrations
from django.db.models import F


def copy_token_forward(apps, schema_editor):
    BankAccount = apps.get_model("company", "BankAccount")
    BankAccount.objects.exclude(nuek_account_id="").update(
        nuek_token=F("nuek_account_id")
    )


def copy_token_backward(apps, schema_editor):
    BankAccount = apps.get_model("company", "BankAccount")
    BankAccount.objects.exclude(nuek_token="").update(
        nuek_account_id=F("nuek_token")
    )


class Migration(migrations.Migration):

    dependencies = [
        ("company", "0050_add_nuek_token_field"),
    ]

    operations = [
        migrations.RunPython(copy_token_forward, copy_token_backward),
    ]
