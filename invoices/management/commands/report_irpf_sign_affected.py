"""List quotes and sales invoices whose stored totals added the IRPF retention.

Quotes and the invoice builders (expediente, delivery order, bulk) used to sum
every tax into ``tax_amount`` and the total, retentions included, where
``SalesInvoice.recalculate_totals`` subtracts them. This command only reports
the documents stored that way so they can be reviewed; it changes nothing.

A quote is corrected the next time it is recalculated, a proforma the next
time it is saved. FINAL invoices keep their issued figures (and may already
have been sent to the AEAT with them).
"""
from decimal import Decimal

from django.contrib.contenttypes.models import ContentType
from django.core.management.base import BaseCommand

from invoices.models import AppliedTax, SalesInvoice
from sales.models import Quote

TOLERANCE = Decimal("0.01")


class Command(BaseCommand):
    help = (
        "Read-only: list quotes and sales invoices whose stored tax and total "
        "added the IRPF retention instead of subtracting it."
    )

    def add_arguments(self, parser):
        parser.add_argument("--company", type=int, help="Restrict to one company id.")

    def handle(self, *args, **options):
        company_id = options.get("company")

        quotes = self._affected(Quote, "total_price", company_id)
        self.stdout.write(f"Quotes ({len(quotes)}):")
        for quote, stored, corrected in quotes:
            self.stdout.write(
                f"  Quote #{quote.pk} {quote.project_name or ''} — "
                f"{quote.client.name if quote.client else '-'} — "
                f"total {stored} (should be {corrected})"
            )

        invoices = self._affected(SalesInvoice, "total_amount", company_id)
        self.stdout.write(f"Sales invoices ({len(invoices)}):")
        for invoice, stored, corrected in invoices:
            self.stdout.write(
                f"  {invoice.display_number} [{invoice.status}] {invoice.date_issued or '-'} — "
                f"{invoice.client.name if invoice.client else '-'} — "
                f"total {stored} (should be {corrected})"
            )

        self.stdout.write(self.style.SUCCESS(
            f"{len(quotes)} quotes and {len(invoices)} sales invoices listed. Nothing was changed."
        ))

    def _affected(self, model, total_field, company_id):
        """``(obj, stored_total, corrected_total)`` for every ``model`` row
        whose stored tax_amount equals its additive taxes PLUS its retentions."""
        ct = ContentType.objects.get_for_model(model)
        retained_ids = set(
            AppliedTax.objects.filter(
                content_type=ct,
                tax_type__behavior="subtract",
                calculated_amount__gt=0,
            ).values_list("object_id", flat=True)
        )
        if not retained_ids:
            return []

        sums = {}  # object_id -> [add, subtract]
        for tax in AppliedTax.objects.filter(
            content_type=ct, object_id__in=retained_ids,
        ).select_related("tax_type"):
            amount = tax.calculated_amount.amount if tax.calculated_amount else Decimal("0.00")
            entry = sums.setdefault(tax.object_id, [Decimal("0.00"), Decimal("0.00")])
            if tax.tax_type.behavior == "add":
                entry[0] += amount
            else:
                entry[1] += amount

        objects = model.objects.filter(pk__in=retained_ids).select_related("client")
        if company_id:
            objects = objects.filter(company_id=company_id)

        affected = []
        for obj in objects.order_by("company_id", "pk"):
            added, retained = sums[obj.pk]
            stored_tax = obj.tax_amount.amount if obj.tax_amount else Decimal("0.00")
            if abs(stored_tax - (added + retained)) > TOLERANCE:
                continue
            stored_total = getattr(obj, total_field).amount
            affected.append((obj, stored_total, stored_total - 2 * retained))
        return affected
