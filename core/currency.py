"""Shared currency helpers."""

import re
from typing import Optional

from moneyed import CURRENCIES

CURRENCY_SYMBOLS = {"€": "EUR", "$": "USD", "£": "GBP", "¥": "JPY"}

CURRENCY_NAMES = {
    "EURO": "EUR", "EUROS": "EUR", "EURS": "EUR",
    "DOLLAR": "USD", "DOLLARS": "USD", "DOLAR": "USD", "DOLARES": "USD",
    "USDOLLAR": "USD", "USDOLLARS": "USD",
    "POUND": "GBP", "POUNDS": "GBP", "STERLING": "GBP", "LIBRA": "GBP", "LIBRAS": "GBP",
    "YEN": "JPY", "FRANC": "CHF", "FRANCS": "CHF",
}


def normalize_currency(currency: Optional[str]) -> Optional[str]:
    """
    Coerce free text ("€", "Euro", "EUROS", "USD $", "(USD)") into a 3-letter
    ISO code that moneyed knows. Currency columns are varchar(3) and Money()
    rejects unknown codes, so returning None lets the caller keep its default.
    """
    if not currency:
        return None
    raw = currency.strip()
    for symbol, code in CURRENCY_SYMBOLS.items():
        if symbol in raw:
            return code
    letters = re.sub(r"[^A-Za-z]", "", raw).upper()
    if letters in CURRENCIES:
        return letters
    if letters in CURRENCY_NAMES:
        return CURRENCY_NAMES[letters]
    for token in re.findall(r"[A-Za-z]+", raw.upper()):
        if token in CURRENCIES:
            return token
        if token in CURRENCY_NAMES:
            return CURRENCY_NAMES[token]
    return None
