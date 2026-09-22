"""Bring every composite item's stock to zero.

A composite item is a recipe rather than goods on a shelf, so from now on it
never moves stock of its own — only the components it is built from do. The
figures already stored for composites were written by the previous rule, which
took the parent out of the warehouse as well as its parts, so the same goods
left twice and the kits drifted (often negative).

Those balances cannot be left alone. An order delivered under the old rule
still carries ``stock_deducted``, and cancelling it after the deploy would
return only the components — the parent's units would stay deducted forever.
Zeroing the composites now closes that gap: whatever the old rule took from a
parent is put back here, and every return from this point on is symmetric.

The compensating movement is recorded rather than deleting the originals, so
the history stays auditable, the same way a cancelled delivery order returns
its goods.
"""
from django.db import migrations
from django.db.models import Sum


def zero_out_composite_stock(apps, schema_editor):
    ItemTransaction = apps.get_model("inventory", "ItemTransaction")
    BillOfMaterialsLine = apps.get_model("inventory", "BillOfMaterialsLine")

    composite_ids = set(
        BillOfMaterialsLine.objects.values_list("parent_id", flat=True)
    )
    if not composite_ids:
        return

    # Stock lives per warehouse, so each one is balanced on its own: a kit
    # sitting at +4 in one nave and -7 in another needs both corrected.
    balances = (
        ItemTransaction.objects
        .filter(item_id__in=composite_ids)
        .values("item_id", "warehouse_id")
        .annotate(total=Sum("quantity_change"))
    )

    ItemTransaction.objects.bulk_create([
        ItemTransaction(
            item_id=row["item_id"],
            warehouse_id=row["warehouse_id"],
            movement_type="ADJUST",
            quantity_change=-row["total"],
            note="Correction: a composite item carries no stock of its own",
        )
        for row in balances
        if row["total"]
    ])


def noop_reverse(apps, schema_editor):
    """Going back would re-create the figures this migration exists to remove."""


class Migration(migrations.Migration):

    dependencies = [
        ("inventory", "0028_inventoryitem_incoming_quantity_and_more"),
    ]

    operations = [
        migrations.RunPython(zero_out_composite_stock, noop_reverse),
    ]
