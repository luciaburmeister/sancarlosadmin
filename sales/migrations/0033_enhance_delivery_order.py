import django.db.models.deletion
import djmoney.models.fields
from decimal import Decimal
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("contenttypes", "0002_remove_content_type_name"),
        ("sales", "0032_add_external_id_to_quote"),
    ]

    operations = [
        # ── DeliveryOrder: add subtotal_amount + notes ──────────────────
        migrations.AddField(
            model_name="deliveryorder",
            name="subtotal_amount_currency",
            field=djmoney.models.fields.CurrencyField(
                choices=[("EUR", "Euro")],
                default="EUR",
                editable=False,
                max_length=3,
            ),
        ),
        migrations.AddField(
            model_name="deliveryorder",
            name="subtotal_amount",
            field=djmoney.models.fields.MoneyField(
                decimal_places=2,
                default=0,
                default_currency="EUR",
                max_digits=12,
                verbose_name="Subtotal",
                help_text="Sum of delivery order item line totals.",
            ),
        ),
        migrations.AddField(
            model_name="deliveryorder",
            name="notes",
            field=models.TextField(blank=True, default="", verbose_name="Notes"),
            preserve_default=False,
        ),

        # ── DeliveryOrderItem: description max_length 255 → 500 ────────
        migrations.AlterField(
            model_name="deliveryorderitem",
            name="description",
            field=models.CharField(max_length=500, verbose_name="Description"),
        ),

        # ── DeliveryOrderItem: pricing fields ──────────────────────────
        migrations.AddField(
            model_name="deliveryorderitem",
            name="unit_price_currency",
            field=djmoney.models.fields.CurrencyField(
                choices=[("EUR", "Euro")],
                default="EUR",
                editable=False,
                max_length=3,
            ),
        ),
        migrations.AddField(
            model_name="deliveryorderitem",
            name="unit_price",
            field=djmoney.models.fields.MoneyField(
                decimal_places=2,
                default=0,
                default_currency="EUR",
                max_digits=12,
                verbose_name="Unit Price",
            ),
        ),
        migrations.AddField(
            model_name="deliveryorderitem",
            name="discount_percentage",
            field=models.DecimalField(
                decimal_places=2,
                default=Decimal("0.00"),
                max_digits=5,
                verbose_name="Discount (%)",
            ),
        ),
        migrations.AddField(
            model_name="deliveryorderitem",
            name="discount_amount_currency",
            field=djmoney.models.fields.CurrencyField(
                choices=[("EUR", "Euro")],
                default="EUR",
                editable=False,
                max_length=3,
            ),
        ),
        migrations.AddField(
            model_name="deliveryorderitem",
            name="discount_amount",
            field=djmoney.models.fields.MoneyField(
                decimal_places=2,
                default=0,
                default_currency="EUR",
                max_digits=12,
                verbose_name="Discount",
            ),
        ),
        migrations.AddField(
            model_name="deliveryorderitem",
            name="line_total_currency",
            field=djmoney.models.fields.CurrencyField(
                choices=[("EUR", "Euro")],
                default="EUR",
                editable=False,
                max_length=3,
            ),
        ),
        migrations.AddField(
            model_name="deliveryorderitem",
            name="line_total",
            field=djmoney.models.fields.MoneyField(
                decimal_places=2,
                default=0,
                default_currency="EUR",
                max_digits=12,
                verbose_name="Line Total",
            ),
        ),

        # ── DeliveryOrderItem: GenericFK for source traceability ───────
        migrations.AddField(
            model_name="deliveryorderitem",
            name="source_content_type",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="delivery_order_items",
                to="contenttypes.contenttype",
                verbose_name="Source Type",
                help_text="Type of the source object (Configuration, QuoteInventoryItem, etc.)",
            ),
        ),
        migrations.AddField(
            model_name="deliveryorderitem",
            name="source_object_id",
            field=models.PositiveBigIntegerField(
                blank=True,
                null=True,
                verbose_name="Source ID",
            ),
        ),
        migrations.AddField(
            model_name="deliveryorderitem",
            name="reference",
            field=models.CharField(
                blank=True,
                default="",
                max_length=200,
                verbose_name="Reference",
                help_text="Item reference code. Auto-filled from source when applicable.",
            ),
        ),

        # ── Index for GenericFK lookups ────────────────────────────────
        migrations.AddIndex(
            model_name="deliveryorderitem",
            index=models.Index(
                fields=["source_content_type", "source_object_id"],
                name="idx_doitem_source",
            ),
        ),
    ]
