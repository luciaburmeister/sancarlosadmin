from django.db import migrations, models
import django.db.models.deletion


def backfill_inventory_item(apps, schema_editor):
    """Denormalise the inventory item onto lines sourced from quote lines.

    Only the pointer is filled; no component lines are created for
    historical delivery orders.
    """
    ContentType = apps.get_model("contenttypes", "ContentType")
    DeliveryOrderItem = apps.get_model("sales", "DeliveryOrderItem")
    QuoteInventoryItem = apps.get_model("sales", "QuoteInventoryItem")

    quote_line_ct = ContentType.objects.filter(
        app_label="sales", model="quoteinventoryitem"
    ).first()
    if quote_line_ct is None:
        return

    lines = DeliveryOrderItem.objects.filter(
        source_content_type=quote_line_ct,
        inventory_item__isnull=True,
        source_object_id__isnull=False,
    )
    quote_line_ids = set(lines.values_list("source_object_id", flat=True))
    item_by_quote_line = dict(
        QuoteInventoryItem.objects.filter(pk__in=quote_line_ids)
        .values_list("pk", "inventory_item_id")
    )
    for line in lines.iterator():
        item_id = item_by_quote_line.get(line.source_object_id)
        if item_id is None:
            continue
        DeliveryOrderItem.objects.filter(pk=line.pk).update(inventory_item_id=item_id)


class Migration(migrations.Migration):

    dependencies = [
        ("contenttypes", "0002_remove_content_type_name"),
        ("inventory", "0028_inventoryitem_incoming_quantity_and_more"),
        ("sales", "0068_alter_quoteinventoryitem_options_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="deliveryorderitem",
            name="inventory_item",
            field=models.ForeignKey(
                blank=True,
                help_text="Product shipped by this line. Picking one on a new line fills the description, reference and price from the item and lists its components underneath.",
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="delivery_order_lines",
                to="inventory.inventoryitem",
                verbose_name="Inventory Item",
            ),
        ),
        migrations.AddField(
            model_name="deliveryorderitem",
            name="parent_line",
            field=models.ForeignKey(
                blank=True,
                help_text="Set on lines that materialise a component of another line's bill of materials. Component lines carry no price and no stock movement of their own.",
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="component_lines",
                to="sales.deliveryorderitem",
                verbose_name="Component of",
            ),
        ),
        migrations.RunPython(backfill_inventory_item, migrations.RunPython.noop),
    ]
