from django.db import migrations
from django.db.models import Q


def merge_cuba_verteo_di_numbers(apps, schema_editor):
    """
    Historical full-cuba MOVEMENTs and their verteo (receipt) got independent
    di_number values from the 0083 backfill (each kept its own row's id-based
    number instead of the pair sharing one, which is what _resolve_di_number
    does for rows created after this feature existed).

    The verteo's number is the one that has already circulated (printed/
    handed over), so it's treated as the source of truth: the movement is
    corrected to match it, not the other way around. Only the first receipt
    linked to a movement counts -- extra split parts are genuinely separate
    DIs and keep their own numbers, same rule as _resolve_di_number.
    """
    AssetTransaction = apps.get_model("assets", "AssetTransaction")

    movements = AssetTransaction.objects.filter(
        Q(transaction_type="movement", data__cubaEstado="Cuba llena")
        | Q(transaction_type="movement", data__vasAVerter="Sí"),
        di_number__isnull=False,
    ).prefetch_related("receipt_links")

    to_update = []
    for movement in movements:
        receipt = sorted(
            movement.receipt_links.all(),
            key=lambda r: (r.part_number, r.created_at, r.pk),
        )
        if not receipt:
            continue
        first_receipt = receipt[0]
        if first_receipt.di_number is None:
            continue
        if first_receipt.di_number == movement.di_number:
            continue
        movement.di_number = first_receipt.di_number
        movement.document_file = ""
        movement.document_generated_at = None
        to_update.append(movement)

    if to_update:
        AssetTransaction.objects.bulk_update(
            to_update, ["di_number", "document_file", "document_generated_at"],
        )

    def _schedule_regeneration():
        from assets.documents import schedule_regeneration

        for movement in to_update:
            schedule_regeneration(movement.pk)

    from django.db import transaction as db_transaction

    db_transaction.on_commit(_schedule_regeneration)


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("assets", "0084_di_number_in_export_templates"),
    ]

    operations = [
        migrations.RunPython(merge_cuba_verteo_di_numbers, noop),
    ]
