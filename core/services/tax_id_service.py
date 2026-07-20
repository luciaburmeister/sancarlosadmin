from typing import Optional


def normalize_tax_id(tax_id: Optional[str]) -> Optional[str]:
    """
    Normalize a tax ID by stripping non-alphanumeric characters, uppercasing,
    and removing the "ES" (Spanish country code) prefix if present.
    """
    if not tax_id:
        return None

    clean = "".join(filter(str.isalnum, tax_id)).upper()
    if clean.startswith("ES"):
        clean = clean[2:]

    return clean or None
