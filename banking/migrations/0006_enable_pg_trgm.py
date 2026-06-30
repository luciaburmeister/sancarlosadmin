from django.contrib.postgres.operations import TrigramExtension
from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("banking", "0005_backfill_bankreconciliation_company"),
    ]

    operations = [
        TrigramExtension(),
    ]
