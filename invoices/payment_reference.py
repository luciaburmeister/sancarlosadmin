"""
The payment reference a payer puts in the concept of a bank transfer so the
movement can be matched to its invoice: ``RF`` + 2 check digits + 8
characters (ISO 11649 creditor reference), e.g. ``RF71 7K3Q X9MZ``.

This module is the only place that knows the format: make one with
``assign_unique()``, read them from a bank concept with ``find_in_text()`` and
print one with ``display()``. Stored compact (``RF717K3QX9MZ``), unique per
company across SalesInvoice and Invoice.
"""
import re
import secrets

from django.apps import apps

# Crockford base32: no I, L, O, U, so nothing reads as another character
ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
BODY_LENGTH = 8
LENGTH = 4 + BODY_LENGTH

REFERENCE_RE = re.compile(rf"RF\d{{2}}[0-9A-Z]{{{BODY_LENGTH}}}")
# Lookahead so windows overlap: in "RF12FRARF717K3QX9MZ" the false window at
# RF12 must not swallow the start of the real reference.
_SCAN_RE = re.compile(rf"(?=({REFERENCE_RE.pattern}))")

# Characters a payer may type instead of the ones the alphabet uses
_TYPO_FIXES = str.maketrans({"O": "0", "I": "1", "L": "1"})


def _mod97(text: str) -> int:
    """ISO 7064 mod 97-10 of ``text``, letters counting as 10 (A) to 35 (Z)."""
    return int("".join(str(int(char, 36)) for char in text)) % 97


def _check_digits(body: str) -> str:
    return f"{98 - _mod97(body + 'RF00'):02d}"


def compact(text: str) -> str:
    """Uppercase ``text`` without the separators banks and people insert."""
    return re.sub(r"[\s\-./]", "", (text or "").upper())


def is_valid(ref: str) -> bool:
    ref = compact(ref)
    if not REFERENCE_RE.fullmatch(ref) or any(char not in ALPHABET for char in ref[4:]):
        return False
    return _mod97(ref[4:] + ref[:4]) == 1


def generate() -> str:
    body = "".join(secrets.choice(ALPHABET) for _ in range(BODY_LENGTH))
    return f"RF{_check_digits(body)}{body}"


def find_in_text(text: str) -> list[str]:
    """Every valid reference in a bank concept, compact and without repeats."""
    found = []
    for candidate in _SCAN_RE.findall(compact(text)):
        ref = candidate[:4] + candidate[4:].translate(_TYPO_FIXES)
        if is_valid(ref) and ref not in found:
            found.append(ref)
    return found


def display(ref: str | None) -> str:
    """``RF717K3QX9MZ`` → ``RF71 7K3Q X9MZ``."""
    if not ref:
        return ""
    return " ".join(ref[i:i + 4] for i in range(0, len(ref), 4))


def assign_unique(company_id) -> str:
    """A new reference no SalesInvoice or Invoice of the company holds yet."""
    models = (apps.get_model("invoices", "SalesInvoice"), apps.get_model("invoices", "Invoice"))
    while True:
        ref = generate()
        if not any(model.objects.filter(company_id=company_id, payment_reference=ref).exists() for model in models):
            return ref
