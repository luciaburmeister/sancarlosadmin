from django.contrib.contenttypes.models import ContentType
from django.core.management.base import BaseCommand
from django.db.models import Q

from configurator.commission.services import CommissionReleaseService
from invoices.models import SalesInvoice, SalesInvoiceStatus
from payments.models import PaymentAllocation


class Command(BaseCommand):
    help = (
        "Rebuild agents' commission releases of every issued invoice and "
        "every invoice with a payment allocated. Safe to re-run: each "
        "invoice is rebuilt."
    )

    def add_arguments(self, parser):
        parser.add_argument("--company", type=int, help="Only this company's invoices.")
        parser.add_argument("--dry-run", action="store_true", help="List the invoices, change nothing.")

    def handle(self, *args, **options):
        # Issued invoices (most are settled by hand, with no payment) and
        # any other with a payment allocated
        paid_ids = PaymentAllocation.objects.filter(
            content_type=ContentType.objects.get_for_model(SalesInvoice),
        ).values_list("object_id", flat=True)
        invoices = SalesInvoice.objects.filter(
            Q(status=SalesInvoiceStatus.FINAL) | Q(pk__in=paid_ids)
        )
        if options["company"]:
            invoices = invoices.filter(company_id=options["company"])
        invoices = invoices.order_by("pk")

        count = invoices.count()
        if options["dry_run"]:
            self.stdout.write(f"{count} invoices would be synced.")
            return

        for index, invoice in enumerate(invoices.select_related("client").iterator(), start=1):
            CommissionReleaseService.sync_for_invoice(invoice)
            if index % 500 == 0:
                self.stdout.write(f"{index}/{count}")
        self.stdout.write(self.style.SUCCESS(f"Synced commission releases on {count} invoices."))
