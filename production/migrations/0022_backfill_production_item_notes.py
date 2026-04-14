from django.db import migrations

def backfill_notes(apps, schema_editor):
    ProductionItem = apps.get_model("production", "ProductionItem")
    Configuration = apps.get_model("configurator", "Configuration")
    ConfigurationItem = apps.get_model("configurator", "ConfigurationItem")
    ContentType = apps.get_model("contenttypes", "ContentType")

    try:
        config_ct = ContentType.objects.get(app_label="configurator", model="configuration")
    except ContentType.DoesNotExist:
        return

    for item in ProductionItem.objects.all():
        ticket = item.ticket
        if ticket.content_type_id == config_ct.id:
            try:
                comp_id = item.data.get("component_id")
                qty = item.data.get("quantity")
                length = item.data.get("length")
                
                if comp_id is None:
                    continue

                c_items = ConfigurationItem.objects.filter(
                    configuration_id=ticket.object_id,
                    component_id=comp_id
                )
                
                c_item = None
                for ci in c_items:
                    # Fuzzy match quantity/length due to float/decimal conversions
                    ci_qty = float(ci.quantity) if ci.quantity else 0.0
                    ci_len = float(ci.length) if ci.length else 1.0
                    
                    if abs(ci_qty - float(qty or 0)) < 0.001 and \
                       abs(ci_len - float(length or 1)) < 0.001:
                        c_item = ci
                        break
                
                if c_item and c_item.note:
                    data = item.data or {}
                    if not data.get("notes"):
                        data["notes"] = c_item.note
                        # Also future-proof by adding the ID if missing
                        if not data.get("configuration_item_id"):
                            data["configuration_item_id"] = c_item.id
                        item.data = data
                        item.save(update_fields=["data"])
            except Exception:
                continue

class Migration(migrations.Migration):
    dependencies = [
        ("production", "0021_productionticket_work_order_pdf"),
        ("configurator", "0052_bomrule_note_expression_configurationitem_note"),
    ]

    operations = [
        migrations.RunPython(backfill_notes, reverse_code=migrations.RunPython.noop),
    ]
