"""
Backfill Quote.discounted_subtotal for all quotes where it is 0 but subtotal > 0.

discounted_subtotal = subtotal_price - discount_amount
"""
from decimal import Decimal
from django.db import migrations


def backfill_discounted_subtotal(apps, schema_editor):
    Quote = apps.get_model("sales", "Quote")
    quotes = Quote.objects.filter(
        discounted_subtotal=0,
        subtotal_price__gt=0,
    )
    updated = 0
    for q in quotes.iterator():
        sub = q.subtotal_price if not hasattr(q.subtotal_price, "amount") else q.subtotal_price.amount
        disc = q.discount_amount if not hasattr(q.discount_amount, "amount") else q.discount_amount.amount
        q.discounted_subtotal = Decimal(str(sub)) - Decimal(str(disc or 0))
        q.save(update_fields=["discounted_subtotal", "discounted_subtotal_currency"])
        updated += 1
    print(f"\n  Backfilled discounted_subtotal on {updated} quotes")


class Migration(migrations.Migration):

    dependencies = [
        ("sales", "0041_remove_client_agent_commission_commissionistagent_and_more"),
    ]

    operations = [
        migrations.RunPython(
            backfill_discounted_subtotal,
            migrations.RunPython.noop,
            elidable=True,
        ),
    ]
