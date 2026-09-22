"""Email provider adapters. See `base.py` for the how-to."""

from .base import (
    EmailProvider,
    InvalidWebhook,
    autodiscover,
    default_provider,
    get_provider,
    register,
    registered_providers,
)

__all__ = [
    "EmailProvider",
    "InvalidWebhook",
    "autodiscover",
    "default_provider",
    "get_provider",
    "register",
    "registered_providers",
]
