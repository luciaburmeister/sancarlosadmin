from django.db import migrations, models


def move_email_kind_to_subtype(apps, schema_editor):
    ConfigurationExportTemplate = apps.get_model("configurator", "ConfigurationExportTemplate")
    ConfigurationExportTemplate.objects.filter(kind="asset_transaction_email").update(
        kind="asset_transaction",
        subtype="email",
    )


def restore_email_kind(apps, schema_editor):
    ConfigurationExportTemplate = apps.get_model("configurator", "ConfigurationExportTemplate")
    ConfigurationExportTemplate.objects.filter(
        kind="asset_transaction",
        subtype="email",
    ).update(kind="asset_transaction_email", subtype="document")


class Migration(migrations.Migration):

    dependencies = [
        ("configurator", "0100_asset_transaction_email_export_kind"),
    ]

    operations = [
        migrations.AddField(
            model_name="configurationexporttemplate",
            name="subtype",
            field=models.CharField(
                choices=[("document", "Document"), ("email", "Email")],
                default="document",
                help_text="Which copy to use when several templates share the same kind.",
                max_length=30,
                verbose_name="Subtype",
            ),
        ),
        migrations.RunPython(move_email_kind_to_subtype, restore_email_kind),
        migrations.AlterField(
            model_name="configurationexporttemplate",
            name="kind",
            field=models.CharField(
                choices=[
                    ("quotation", "Quotation"),
                    ("production_order", "Production Order"),
                    ("production_ticket", "Production Ticket"),
                    ("sales_invoice", "Sales Invoice"),
                    ("consolidated_quotation", "Consolidated Quotation"),
                    ("receival_slip", "Receival Slip"),
                    ("asset_transaction", "Asset Transaction"),
                    ("delivery_order", "Delivery Order"),
                    ("repair_order", "Repair Order"),
                    ("quote_request", "Quote Request"),
                    ("purchase_order", "Purchase Order"),
                ],
                default="quotation",
                max_length=30,
                verbose_name="Kind",
            ),
        ),
    ]
