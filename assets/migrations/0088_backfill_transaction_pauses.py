from django.db import migrations


def backfill_transaction_pauses(apps, schema_editor):
    """
    Historical equivalent of the daily assets.tasks.detect_transaction_pauses
    task: gives a second DI to every full-cuba movement (already dated on or
    after DI_NUMBERING_START_DATE, so it has a di_number from 0083) that sat
    paused across a day boundary before being resolved -- whether it was
    eventually dumped on a later day, or is still unresolved today. Reuses
    the exact same detection/creation function the daily task calls, just
    run once for the past instead of going forward.
    """
    from assets.services.transaction_pause import TransactionPauseService

    TransactionPauseService.generate_due_pauses()


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        # Not 0087 directly: this backfill creates real rows through the
        # live AssetTransactionPause.save(), so every field it currently
        # defines -- including date (0089) and date_overridden (0090) --
        # must already have a matching column by the time this runs on a
        # fresh database, or the INSERT breaks (see assets.services.
        # di_numbering for why the SELECT side of this is hardened the
        # same way).
        ("assets", "0090_add_pause_date_overridden"),
    ]

    operations = [
        migrations.RunPython(backfill_transaction_pauses, noop),
    ]
