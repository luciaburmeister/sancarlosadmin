from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("configurator", "0099_alter_configurationexporttemplate_kind"),
    ]

    operations = [
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
                    ("asset_transaction_email", "Asset Transaction Email"),
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
