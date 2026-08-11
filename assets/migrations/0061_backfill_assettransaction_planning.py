from django.db import migrations


def backfill_planning(apps, schema_editor):
    AssetTransaction = apps.get_model("assets", "AssetTransaction")
    AssetPlanning = apps.get_model("assets", "AssetPlanning")
    qs = AssetTransaction.objects.filter(planning__isnull=True).exclude(data__planning_id__isnull=True)
    for tx in qs.iterator():
        raw_id = tx.data.get("planning_id")
        if not raw_id:
            continue
        try:
            planning_id = int(raw_id)
        except (TypeError, ValueError):
            continue
        planning = AssetPlanning.objects.filter(id=planning_id, company_id=tx.company_id).first()
        if planning:
            tx.planning_id = planning.id
            tx.save(update_fields=["planning"])


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('assets', '0060_assettransaction_planning'),
    ]

    operations = [
        migrations.RunPython(backfill_planning, noop_reverse),
    ]
