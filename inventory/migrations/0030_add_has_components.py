from django.db import migrations, models


def backfill_has_components(apps, schema_editor):
    """Set the flag on the items that already have a bill of materials.

    From here on inventory.signals keeps it in step; this only covers the rows
    that existed before the column did.
    """
    InventoryItem = apps.get_model("inventory", "InventoryItem")
    BillOfMaterialsLine = apps.get_model("inventory", "BillOfMaterialsLine")

    parent_ids = set(BillOfMaterialsLine.objects.values_list("parent_id", flat=True))
    if parent_ids:
        InventoryItem.objects.filter(pk__in=parent_ids).update(has_components=True)


def noop_reverse(apps, schema_editor):
    """The column goes with the field, so there is nothing to undo."""


class Migration(migrations.Migration):

    dependencies = [
        ('inventory', '0029_zero_out_composite_stock'),
    ]

    operations = [
        migrations.AddField(
            model_name='inventoryitem',
            name='has_components',
            field=models.BooleanField(db_index=True, default=False, editable=False, help_text='Whether this item is assembled from others. Set from the bill of materials, never by hand.', verbose_name='Composite Item'),
        ),
        migrations.RunPython(backfill_has_components, noop_reverse),
    ]
