from django.db import migrations


def backfill_order(apps, schema_editor):
    """
    Number groups left at the default order=0, so new ones append instead of
    tying with them. Quotes with real positions were reordered by hand and are
    skipped.
    """
    QuoteConfigurationGroup = apps.get_model("sales", "QuoteConfigurationGroup")

    quote_ids = (
        QuoteConfigurationGroup.objects.values_list("quote_id", flat=True).distinct()
    )
    for quote_id in quote_ids:
        groups = list(
            QuoteConfigurationGroup.objects.filter(quote_id=quote_id).order_by("id")
        )
        if any(group.order for group in groups):
            continue
        for position, group in enumerate(groups):
            group.order = position
        QuoteConfigurationGroup.objects.bulk_update(groups, ["order"])


class Migration(migrations.Migration):

    dependencies = [
        ("sales", "0060_alter_quoteinventoryitem_options_and_more"),
    ]

    operations = [
        # Reversing would have to guess which positions were manual.
        migrations.RunPython(backfill_order, migrations.RunPython.noop),
    ]
