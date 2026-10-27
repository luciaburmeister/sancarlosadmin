from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _

# EAN-8, UPC-A, EAN-13 and GTIN-14: the lengths GS1 defines a check digit for.
GTIN_LENGTHS = (8, 12, 13, 14)


def gtin_check_digit_ok(code):
    """GS1 mod-10: weights 3,1,3,1… from the digit left of the check digit."""
    digits = [int(c) for c in code]
    body, check = digits[:-1], digits[-1]
    total = sum(d * (3 if i % 2 == 0 else 1) for i, d in enumerate(reversed(body)))
    return (10 - total % 10) % 10 == check


def validate_barcode(value):
    """Any format is accepted; a GTIN-shaped value must have a valid check digit."""
    value = (value or "").strip()
    if value.isdigit() and len(value) in GTIN_LENGTHS and not gtin_check_digit_ok(value):
        raise ValidationError(
            _("Invalid check digit for a %(length)s-digit EAN/GTIN barcode."),
            code="invalid_barcode",
            params={"length": len(value)},
        )
