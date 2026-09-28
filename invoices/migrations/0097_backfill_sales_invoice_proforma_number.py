from django.db import migrations
from django.utils import timezone


def backfill_proforma_numbers(apps, schema_editor):
    """
    Number every sales invoice created in the ERP, per company, in the order
    it was drafted — finalized and cancelled ones included, so the sequence
    has no gaps. External (imported) invoices never were proformas and stay
    unnumbered. The {year} placeholder takes the year the proforma was drafted.
    """
    Company = apps.get_model("company", "Company")
    SalesInvoice = apps.get_model("invoices", "SalesInvoice")

    for company in Company.objects.all().iterator():
        invoices = list(
            SalesInvoice.objects.filter(
                company=company,
                is_external=False,
                proforma_number__isnull=True,
            ).order_by("created_at", "id")
        )
        if not invoices:
            continue

        fmt = company.proforma_number_format or "PF-{year}-{number}"
        last_id = (
            SalesInvoice.objects.filter(company=company, proforma_incremental_id__isnull=False)
            .order_by("-proforma_incremental_id")
            .values_list("proforma_incremental_id", flat=True)
            .first()
        ) or 0

        for offset, invoice in enumerate(invoices, start=1):
            next_id = last_id + offset
            year = timezone.localtime(invoice.created_at).year
            invoice.proforma_incremental_id = next_id
            invoice.proforma_number = fmt.format(year=year, number=f"{next_id:05d}")

        SalesInvoice.objects.bulk_update(
            invoices, ["proforma_number", "proforma_incremental_id"], batch_size=500
        )


class Migration(migrations.Migration):

    dependencies = [
        ("company", "0085_company_proforma_number_format"),
        ("invoices", "0096_historicalsalesinvoice_proforma_incremental_id_and_more"),
    ]

    operations = [
        migrations.RunPython(backfill_proforma_numbers, migrations.RunPython.noop),
    ]
