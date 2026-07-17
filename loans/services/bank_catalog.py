from banking.services.arcopay_import import ArcopayService
from loans.models import Bank


def sync_banks_from_arcopay(country_code: str = "ES") -> int:
    """Upsert loans.Bank from Arcopay's list of supported banks. Read-only
    against banking — only calls list_supported_banks(), never writes there."""
    banks = ArcopayService.list_supported_banks(country_code=country_code)
    count = 0
    for b in banks:
        service = b.get("service", "")
        name = b.get("fullname", "")
        if not service or not name:
            continue
        Bank.objects.update_or_create(
            arcopay_service=service,
            defaults={
                "name": name,
                "swift_bic": b.get("swift", ""),
                "country": b.get("countryCode", "") or None,
                "logo_url": b.get("imageSVG") or b.get("image") or "",
            },
        )
        count += 1
    return count