from importlib import import_module

from django.db import migrations


merge_exact_slots = import_module(
    "production.migrations.0048_merge_per_resource_sessions"
).merge_sessions


class Migration(migrations.Migration):

    dependencies = [
        ("production", "0049_productionticket_stage_order"),
    ]

    operations = [
        # 0048 may already be applied on installations where a duplicate was
        # later introduced. Re-running its idempotent exact-slot merge makes
        # the constraint below deploy safely with real planning data.
        migrations.RunPython(merge_exact_slots, migrations.RunPython.noop),
    ]
