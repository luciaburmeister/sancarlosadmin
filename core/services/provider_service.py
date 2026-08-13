from typing import Optional

from django.db import connection, transaction

from company.models import Company
from materials.models import Provider
from core.services.tax_id_service import normalize_tax_id


def get_or_create_provider(
    vendor_tax_id: Optional[str],
    vendor_name: Optional[str],
    company: Optional[Company],
    sender_email: Optional[str] = None,
) -> Optional[Provider]:
    """
    Lookup a Provider by tax ID, scoped to company. Creates a new Provider if
    no match is found and a company is known. Tax ID is the sole identity
    signal — email is never used to find or disambiguate a provider, since
    a mailbox isn't guaranteed to belong to a single vendor.

    Shared by any module that extracts vendor info via OCR (invoices, expense
    tickets, ...) so provider matching/creation behaves identically everywhere.
    """
    clean_tax_id = normalize_tax_id(vendor_tax_id)
    if not clean_tax_id or not company:
        return None

    with transaction.atomic():
        # Concurrent invoices from the same not-yet-onboarded vendor can hit
        # this function at the same time, both miss the lookup below, and
        # both try to create the provider. Serialize on a transaction-scoped
        # advisory lock keyed by (company, tax id) so the second caller waits
        # for the first to commit, then finds the row it just created instead
        # of racing it. Released automatically when this transaction ends.
        cursor = connection.cursor()
        cursor.execute(
            "SELECT pg_advisory_xact_lock(hashtext(%s))",
            [f"provider_tax_identifier:{company.pk}:{clean_tax_id}"],
        )

        provider = Provider.objects.filter(
            tax_identifier__iexact=clean_tax_id,
            company=company,
        ).first()

        if not provider:
            provider = Provider.objects.create(
                name=vendor_name or f"New Provider {clean_tax_id}",
                tax_identifier=clean_tax_id,
                company=company,
            )

    # Enrichment only, never a lookup key: record that this address has sent
    # invoices for this (already tax-ID-resolved) provider, as a hint for
    # whoever reviews it later.
    if sender_email:
        sender_email = sender_email.lower()
        if sender_email not in provider.associated_emails:
            provider.associated_emails.append(sender_email)
            provider.save(update_fields=["associated_emails"])

    return provider
