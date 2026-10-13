"""Fill Payment.provider from the invoices each payment was allocated to.

A payment whose allocations all point at invoices of one provider gets that
provider. A payment split across invoices of different providers is left
empty for someone to settle by hand rather than guessed. Payments with no
purchase-invoice allocation (sales invoices, loans, expense claims, payments
on account) are untouched.
"""
from collections import defaultdict

from django.db import migrations


def backfill_payment_provider(apps, schema_editor):
    ContentType = apps.get_model("contenttypes", "ContentType")
    Payment = apps.get_model("payments", "Payment")
    PaymentAllocation = apps.get_model("payments", "PaymentAllocation")
    Invoice = apps.get_model("invoices", "Invoice")

    invoice_ct = ContentType.objects.filter(
        app_label="invoices", model="invoice"
    ).first()
    if invoice_ct is None:
        return  # Fresh database: no content types, so nothing to backfill.

    allocations = PaymentAllocation.objects.filter(
        content_type=invoice_ct, payment__provider__isnull=True
    ).values_list("payment_id", "object_id")

    invoice_ids = {object_id for _, object_id in allocations}
    invoice_provider = dict(
        Invoice.objects.filter(pk__in=invoice_ids).values_list("pk", "provider_id")
    )

    providers_per_payment = defaultdict(set)
    for payment_id, object_id in allocations:
        provider_id = invoice_provider.get(object_id)
        if provider_id:
            providers_per_payment[payment_id].add(provider_id)

    payments_per_provider = defaultdict(list)
    for payment_id, provider_ids in providers_per_payment.items():
        if len(provider_ids) == 1:
            payments_per_provider[provider_ids.pop()].append(payment_id)

    for provider_id, payment_ids in payments_per_provider.items():
        Payment.objects.filter(pk__in=payment_ids).update(provider_id=provider_id)


class Migration(migrations.Migration):

    dependencies = [
        ("payments", "0008_payment_provider"),
        ("invoices", "0099_merge_20261005_1730"),
        ("contenttypes", "0002_remove_content_type_name"),
    ]

    operations = [
        migrations.RunPython(backfill_payment_provider, migrations.RunPython.noop),
    ]
