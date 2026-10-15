"""Shared fixtures for the digest/report email tests: companies and one-line invoices."""
from djmoney.money import Money

from company.models import Company
from invoices.models import Invoice, SalesInvoice, SalesInvoiceStatus
from materials.models import Provider
from sales.models import Client


def make_company(*, name="Alpha", **fields):
    """Create a Company; ``fields`` carries the digest recipients being tested."""
    return Company.objects.create(name=name, **fields)


def make_purchase_invoice(*, company, number, total="100.00", provider=None, **fields):
    """Create a purchase Invoice in EUR, from a fresh ``Proveedor <number>`` unless given."""
    provider = provider or Provider.objects.create(company=company, name=f"Proveedor {number}")
    return Invoice.objects.create(
        company=company,
        provider=provider,
        invoice_number=number,
        total_amount=Money(total, "EUR"),
        **fields,
    )


def make_sales_invoice(*, company, number, total="200.00", client=None, **fields):
    """Create a final SalesInvoice in EUR, for a fresh ``Cliente <number>`` unless given."""
    client = client or Client.objects.create(company=company, name=f"Cliente {number}")
    fields.setdefault("status", SalesInvoiceStatus.FINAL)
    return SalesInvoice.objects.create(
        company=company,
        client=client,
        invoice_number=number,
        total_amount=Money(total, "EUR"),
        **fields,
    )
