from django.db import migrations


def backfill_payment_references(apps, schema_editor):
    """
    Give every existing invoice the payment reference new ones get on
    creation, so they can be quoted in bank concepts right away: sales
    invoices except external ones, purchase invoices that have a company.
    References are unique per company across both tables.
    """
    # Only the pure generator: the format is fixed by ISO 11649
    from invoices.payment_reference import generate

    Company = apps.get_model("company", "Company")
    SalesInvoice = apps.get_model("invoices", "SalesInvoice")
    Invoice = apps.get_model("invoices", "Invoice")

    for company in Company.objects.all().iterator():
        sales = list(
            SalesInvoice.objects.filter(company=company, is_external=False, payment_reference__isnull=True)
            .only("id", "payment_reference")
        )
        purchases = list(
            Invoice.objects.filter(company=company, payment_reference__isnull=True)
            .only("id", "payment_reference")
        )
        if not sales and not purchases:
            continue

        taken = set(
            SalesInvoice.objects.filter(company=company, payment_reference__isnull=False)
            .values_list("payment_reference", flat=True)
        ) | set(
            Invoice.objects.filter(company=company, payment_reference__isnull=False)
            .values_list("payment_reference", flat=True)
        )
        for invoice in (*sales, *purchases):
            ref = generate()
            while ref in taken:
                ref = generate()
            taken.add(ref)
            invoice.payment_reference = ref

        SalesInvoice.objects.bulk_update(sales, ["payment_reference"], batch_size=1000)
        Invoice.objects.bulk_update(purchases, ["payment_reference"], batch_size=1000)


class Migration(migrations.Migration):

    dependencies = [
        ("invoices", "0104_payment_reference"),
    ]

    operations = [
        migrations.RunPython(backfill_payment_references, migrations.RunPython.noop),
    ]
