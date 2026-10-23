from typing import Optional

from django.db import connection, transaction

from company.models import Company
from core.services.tax_id_service import normalize_tax_id
from sales.models import Client


def get_or_create_client(
    tax_id: Optional[str],
    name: Optional[str],
    company: Optional[Company],
    **defaults,
) -> Optional[Client]:
    """
    Lookup a Client by tax ID, scoped to company, creating it with
    ``defaults`` when there is none. Tax ID is the sole identity signal, the
    same as get_or_create_provider; an existing client is returned as is.
    """
    clean_tax_id = normalize_tax_id(tax_id)
    if not clean_tax_id or not company:
        return None

    with transaction.atomic():
        # Two requests for the same new client must not both create it
        cursor = connection.cursor()
        cursor.execute(
            "SELECT pg_advisory_xact_lock(hashtext(%s))",
            [f"client_tax_identifier:{company.pk}:{clean_tax_id}"],
        )

        client = Client.objects.filter(
            tax_identifier__iexact=clean_tax_id,
            company=company,
        ).first()

        if not client:
            client = Client.objects.create(
                name=name or f"New Client {clean_tax_id}",
                tax_identifier=clean_tax_id,
                company=company,
                **defaults,
            )

    return client
