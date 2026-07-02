"""
Backfill Quote.total_cost for all existing quotes.

total_cost = sum(config.total_cost) + sum(inventory_item.cost_price × qty)
"""
from decimal import Decimal

from django.db import migrations


def backfill_total_cost(apps, schema_editor):
    Quote = apps.get_model("sales", "Quote")

    quotes = Quote.objects.all()
    updated = 0

    for q in quotes:
        config_cost = Decimal("0.00")
        for c in q.configurations.all():
            if hasattr(c, "total_cost") and c.total_cost:
                amt = c.total_cost if not hasattr(c.total_cost, "amount") else c.total_cost.amount
                config_cost += Decimal(str(amt or 0))

        inv_cost = Decimal("0.00")
        for qi in q.inventory_items.select_related("inventory_item").all():
            cp = qi.inventory_item.cost_price
            if cp:
                cp_amt = cp if not hasattr(cp, "amount") else cp.amount
                if cp_amt:
                    inv_cost += Decimal(str(cp_amt)) * qi.quantity

        total = config_cost + inv_cost
        if total > 0:
            q.total_cost = total
            q.save(update_fields=["total_cost", "total_cost_currency"])
            updated += 1

    print(f"\n  Backfilled total_cost on {updated} quotes")


class Migration(migrations.Migration):

    dependencies = [
        ("sales", "0045_quote_total_cost"),
    ]

    operations = [
        migrations.RunPython(
            backfill_total_cost,
            migrations.RunPython.noop,
        ),
    ]
