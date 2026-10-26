from django.db import migrations

OLD = '{{ transaction.id|stringformat:"05d" }}'
NEW = '{{ document_number|stringformat:"05d" }}'


def use_document_number(apps, schema_editor):
    """The DI "Nº" line used to print the raw transaction.id, which breaks
    for a split receipt (every part shares the same parent transaction, so
    they'd all print the same number). assets.documents now passes the
    correct per-document number as `document_number` in the render context
    (see _render_document_pdf) -- swap any export template still reading
    transaction.id directly to use it instead. Scoped by the exact template
    text, not by company, so it applies to any company using this pattern,
    today or in the future."""
    ConfigurationExportTemplate = apps.get_model("configurator", "ConfigurationExportTemplate")
    for template in ConfigurationExportTemplate.objects.filter(html__contains=OLD):
        template.html = template.html.replace(OLD, NEW)
        template.save(update_fields=["html"])


def use_transaction_id(apps, schema_editor):
    ConfigurationExportTemplate = apps.get_model("configurator", "ConfigurationExportTemplate")
    for template in ConfigurationExportTemplate.objects.filter(html__contains=NEW):
        template.html = template.html.replace(NEW, OLD)
        template.save(update_fields=["html"])


class Migration(migrations.Migration):

    dependencies = [
        ("assets", "0083_backfill_di_numbers"),
        ("configurator", "0109_configurationexporttemplate_language"),
    ]

    operations = [
        migrations.RunPython(use_document_number, use_transaction_id),
    ]
