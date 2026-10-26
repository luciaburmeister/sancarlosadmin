from django.db import migrations
from django.db.models import F, Max, Q

from assets.constants import DI_NUMBERING_START_DATE


def backfill_di_numbers(apps, schema_editor):
    """
    Assigns di_number to every pre-existing DI-worthy row dated on or after
    DI_NUMBERING_START_DATE, preserving exactly the number its document
    already showed before this feature existed (transaction.id, see
    assets.documents) -- not a renumbering. Nothing dated before that cutoff
    ever gets a di_number.

    AssetTransaction.id is always globally unique, so a straight copy can
    never collide with another row: one bulk UPDATE, no loop needed.

    AssetTransactionReceipt is one receipt per transaction before this
    feature (so transaction_id is what its document showed), except for a
    handful already split during this feature's own development, which
    share one transaction_id across several rows. Only the first part of
    such a family can keep that legacy number -- the others never had a
    number of their own to preserve (the pre-fix bug was that every part
    showed the same one), so they get fresh numbers above any id that could
    ever collide, continuing the same per-company sequence.
    """
    AssetTransaction = apps.get_model("assets", "AssetTransaction")
    AssetTransactionReceipt = apps.get_model("assets", "AssetTransactionReceipt")

    AssetTransaction.objects.filter(
        Q(transaction_type="porte_load")
        | Q(transaction_type="movement", data__cubaEstado="Cuba llena")
        | Q(transaction_type="movement", data__vasAVerter="Sí"),
        date__date__gte=DI_NUMBERING_START_DATE,
        di_number__isnull=True,
    ).update(di_number=F("id"))

    seen_transaction_ids = set()
    next_extra_number_by_company = {}
    receipts = AssetTransactionReceipt.objects.filter(
        transaction__date__date__gte=DI_NUMBERING_START_DATE,
        di_number__isnull=True,
    ).select_related("transaction").order_by("transaction_id", "part_number", "id")
    for receipt in receipts:
        transaction_id = receipt.transaction_id
        if transaction_id not in seen_transaction_ids:
            seen_transaction_ids.add(transaction_id)
            receipt.di_number = transaction_id
        else:
            company_id = receipt.transaction.company_id
            number = next_extra_number_by_company.get(company_id)
            if number is None:
                highest_id = AssetTransaction.objects.filter(
                    company_id=company_id,
                ).aggregate(Max("id"))["id__max"] or 0
                number = highest_id + 1
            receipt.di_number = number
            next_extra_number_by_company[company_id] = number + 1
        receipt.save(update_fields=["di_number"])


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("assets", "0082_assettransaction_di_number_and_more"),
    ]

    operations = [
        migrations.RunPython(backfill_di_numbers, noop),
    ]
