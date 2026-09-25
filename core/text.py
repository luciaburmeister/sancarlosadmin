"""Shared text normalization for values that are later compared to catalog names."""

import re
import unicodedata

_UNICODE_ESCAPE = re.compile(r"\\u([0-9a-fA-F]{4})|\\U([0-9a-fA-F]{8})")


def normalize_match_text(value):
    """Decode JSON unicode escapes and use composed characters."""
    if not isinstance(value, str) or not value:
        return value
    if _UNICODE_ESCAPE.search(value):
        value = _UNICODE_ESCAPE.sub(
            lambda match: chr(int(match.group(1) or match.group(2), 16)),
            value,
        )
    return unicodedata.normalize("NFC", value)


def normalize_match_data(value):
    """Normalize strings inside JSON-like dicts and lists. Other values pass through."""
    if isinstance(value, str):
        return normalize_match_text(value)
    if isinstance(value, dict):
        return {key: normalize_match_data(item) for key, item in value.items()}
    if isinstance(value, list):
        return [normalize_match_data(item) for item in value]
    return value
