from django.db import migrations


def backfill_date_overridden(apps, schema_editor):
    """Pre-existing rows never got a chance to flag a manually-corrected
    date: under the old save() (always `self.date or movement.date`), the
    only explanation for one differing from both its own movement's date
    and its resolving receipt's date is a past superuser edit via the admin
    -- the sole write path for it. Flag those as overridden so they keep
    whatever a human set. A row that already matches movement.date or the
    receipt's date isn't an override (0088 running on a database that
    never had this feature creates fresh rows already resolution-aware, via
    the live, current save() -- those legitimately differ from movement.date
    without anyone having touched them); such rows just get a chance to
    pick up the receipt's date right away, instead of waiting for their
    next unrelated save()."""
    AssetTransactionPause = apps.get_model("assets", "AssetTransactionPause")
    AssetTransactionReceipt = apps.get_model("assets", "AssetTransactionReceipt")

    overridden, resynced = [], []
    for pause in AssetTransactionPause.objects.select_related("movement"):
        receipt = AssetTransactionReceipt.objects.filter(
            movement_transaction_id=pause.movement_id,
        ).select_related("transaction").order_by("created_at").first()
        resolution_date = receipt.transaction.date if receipt is not None else None

        if pause.date not in (pause.movement.date, resolution_date):
            pause.date_overridden = True
            overridden.append(pause)
        elif resolution_date is not None and pause.date != resolution_date:
            pause.date = resolution_date
            resynced.append(pause)

    if overridden:
        AssetTransactionPause.objects.bulk_update(overridden, ["date_overridden"])
    if resynced:
        AssetTransactionPause.objects.bulk_update(resynced, ["date"])


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        # After 0088: this needs pause rows (and their receipts) to already
        # exist to decide, per row, whether to flag it overridden or resync
        # its date.
        ("assets", "0088_backfill_transaction_pauses"),
    ]

    operations = [
        migrations.RunPython(backfill_date_overridden, noop),
    ]
