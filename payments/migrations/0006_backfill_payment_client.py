"""Fill Payment.client from the sales invoices each payment was allocated to.

A payment whose allocations all point at sales invoices of one client gets
that client. A payment split across invoices of different clients is left
empty for someone to settle by hand rather than guessed. Payments with no
sales-invoice allocation (purchase invoices, loans, expense claims, payments
on account) are untouched.
"""
from collections import defaultdict

from django.db import migrations


def backfill_payment_client(apps, schema_editor):
    ContentType = apps.get_model("contenttypes", "ContentType")
    Payment = apps.get_model("payments", "Payment")
    PaymentAllocation = apps.get_model("payments", "PaymentAllocation")
    SalesInvoice = apps.get_model("invoices", "SalesInvoice")

    sales_invoice_ct = ContentType.objects.filter(
        app_label="invoices", model="salesinvoice"
    ).first()
    if sales_invoice_ct is None:
        return  # Fresh database: no content types, so nothing to backfill.

    allocations = PaymentAllocation.objects.filter(
        content_type=sales_invoice_ct, payment__client__isnull=True
    ).values_list("payment_id", "object_id")

    invoice_ids = {object_id for _, object_id in allocations}
    invoice_client = dict(
        SalesInvoice.objects.filter(pk__in=invoice_ids).values_list("pk", "client_id")
    )

    clients_per_payment = defaultdict(set)
    for payment_id, object_id in allocations:
        client_id = invoice_client.get(object_id)
        if client_id:
            clients_per_payment[payment_id].add(client_id)

    payments_per_client = defaultdict(list)
    for payment_id, client_ids in clients_per_payment.items():
        if len(client_ids) == 1:
            payments_per_client[client_ids.pop()].append(payment_id)

    for client_id, payment_ids in payments_per_client.items():
        Payment.objects.filter(pk__in=payment_ids).update(client_id=client_id)


class Migration(migrations.Migration):

    dependencies = [
        ("payments", "0005_payment_client"),
        ("invoices", "0093_alter_invoice_options"),
        ("contenttypes", "0002_remove_content_type_name"),
    ]

    operations = [
        # Reverse is a no-op: dropping the column in 0005 discards the data anyway.
        migrations.RunPython(backfill_payment_client, migrations.RunPython.noop),
    ]
