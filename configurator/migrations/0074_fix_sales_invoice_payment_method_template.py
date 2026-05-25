"""Remove duplicate top payment block; use payment_method_display in Vencimientos."""

import re

from django.db import migrations

from configurator.services.export.helpers import strip_wrappers


def _fix_template_html(raw_html: str) -> str:
    if not raw_html:
        return raw_html

    cleaned = strip_wrappers(raw_html)

    cleaned = re.sub(
        r"\{%\s*if\s+payment_method\s*%\}.*?\{%\s*endif\s*%\}",
        "",
        cleaned,
        count=1,
        flags=re.DOTALL | re.IGNORECASE,
    )

    cleaned = cleaned.replace(
        "<strong>Forma de pago:</strong> Transferencia",
        "<strong>Forma de pago:</strong> {{ payment_method_display }}",
    )
    cleaned = cleaned.replace(
        "<strong>Forma de pago:</strong> transfer",
        "<strong>Forma de pago:</strong> {{ payment_method_display }}",
    )
    cleaned = cleaned.replace(
        "{{ client.get_payment_method_display }}",
        "{{ payment_method_display }}",
    )
    cleaned = cleaned.replace(
        "{{ client.payment_method }}",
        "{{ payment_method_display }}",
    )

    return cleaned


def fix_sales_invoice_payment_templates(apps, schema_editor):
    ConfigurationExportTemplate = apps.get_model(
        "configurator", "ConfigurationExportTemplate"
    )
    for template in ConfigurationExportTemplate.objects.filter(
        kind="sales_invoice", is_active=True
    ):
        updated = _fix_template_html(template.html or "")
        if updated != (template.html or ""):
            template.html = updated
            template.save(update_fields=["html"])


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("configurator", "0073_backfill_project_file_from_tickets"),
        ("invoices", "0049_salesinvoice_payment_method"),
    ]

    operations = [
        migrations.RunPython(fix_sales_invoice_payment_templates, noop),
    ]
