"""
Recalculate ProjectFile cached invoicing fields after quote backfill.

Depends on sales.0042 which backfills Quote.discounted_subtotal.
"""
from decimal import Decimal
from django.db import migrations
from django.db.models import Sum


def recalculate_project_file_totals(apps, schema_editor):
    ProjectFile = apps.get_model("management", "ProjectFile")
    Quote = apps.get_model("sales", "Quote")
    Configuration = apps.get_model("configurator", "Configuration")
    SalesInvoice = apps.get_model("invoices", "SalesInvoice")

    updated = 0
    for pf in ProjectFile.objects.all().iterator():
        # Accepted quotes subtotal (pre-tax, post-discount)
        quote_sub = (
            Quote.objects.filter(
                project_file=pf, status="accepted",
            ).aggregate(total=Sum("discounted_subtotal"))["total"]
            or Decimal("0.00")
        )
        if hasattr(quote_sub, "amount"):
            quote_sub = quote_sub.amount

        # Direct configurations subtotal (pre-tax)
        config_sub = (
            Configuration.objects.filter(
                project_file=pf,
                quote__isnull=True,
                status__in=["shop", "completed", "converted", "production_done"],
            ).aggregate(total=Sum("subtotal_price"))["total"]
            or Decimal("0.00")
        )
        if hasattr(config_sub, "amount"):
            config_sub = config_sub.amount

        invoiceable = Decimal(str(quote_sub)) + Decimal(str(config_sub))

        # Sales invoice subtotals (pre-tax)
        invoiced = (
            SalesInvoice.objects.filter(
                project_file=pf,
            ).aggregate(total=Sum("subtotal_amount"))["total"]
            or Decimal("0.00")
        )
        if hasattr(invoiced, "amount"):
            invoiced = invoiced.amount
        invoiced = Decimal(str(invoiced))

        pct = (
            (invoiced / invoiceable * Decimal("100")).quantize(Decimal("0.01"))
            if invoiceable > 0 else Decimal("0.00")
        )

        pf.total_amount = invoiceable
        pf.total_invoiced = invoiced
        pf.percentage_invoiced = pct
        pf.save(update_fields=[
            "total_amount", "total_amount_currency",
            "total_invoiced", "total_invoiced_currency",
            "percentage_invoiced",
        ])
        updated += 1

    print(f"\n  Recalculated invoicing totals on {updated} project files")


class Migration(migrations.Migration):

    dependencies = [
        ("management", "0009_add_invoicing_cache_to_projectfile"),
        ("sales", "0042_backfill_discounted_subtotal"),
        ("invoices", "0061_add_error_to_incominginvoice"),
        ("configurator", "0083_alter_agentcommissionrule_options_and_more"),
    ]

    operations = [
        migrations.RunPython(
            recalculate_project_file_totals,
            migrations.RunPython.noop,
            elidable=True,
        ),
    ]
