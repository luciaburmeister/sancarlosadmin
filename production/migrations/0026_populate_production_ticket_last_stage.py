from django.db import migrations

def populate_last_stage(apps, schema_editor):
    # Fresh installs must not load the live model before later columns exist.
    if not apps.get_model("production", "ProductionTicket").objects.exists():
        return
    from production.models import ProductionTicket
    for ticket in ProductionTicket.objects.all():
        ticket.recompute_status()

class Migration(migrations.Migration):

    dependencies = [
        ('production', '0025_productionticket_last_stage_label_and_more'),
    ]

    operations = [
        migrations.RunPython(populate_last_stage, reverse_code=migrations.RunPython.noop),
    ]
