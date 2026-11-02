"""
Encryption for secrets the ERP stores in its database, such as the access
tokens of connected integrations.

Keys come from ``settings.ENCRYPTION_KEYS`` (Fernet keys), never from
``SECRET_KEY``: rotating ``SECRET_KEY`` must not make stored secrets
unreadable. The first key encrypts and every listed key decrypts, so a key is
rotated by putting a new one first and keeping the old one until the values it
encrypted have been saved again.

``SecretCipher.decrypt`` raises ``cryptography.fernet.InvalidToken`` when no listed
key matches the value.
"""

from cryptography.fernet import Fernet, MultiFernet
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured


class SecretCipher:
    """Encrypts and decrypts stored secrets with ``settings.ENCRYPTION_KEYS``."""

    @staticmethod
    def cipher() -> MultiFernet:
        # Read on every call rather than cached, so a key change in settings (or
        # override_settings in tests) applies immediately.
        keys = getattr(settings, "ENCRYPTION_KEYS", None) or []
        if not keys:
            raise ImproperlyConfigured(
                "ENCRYPTION_KEYS is empty. Set the ENCRYPTION_KEYS environment variable "
                "to one or more comma-separated Fernet keys."
            )
        try:
            return MultiFernet([Fernet(key) for key in keys])
        except (TypeError, ValueError) as exc:
            raise ImproperlyConfigured("ENCRYPTION_KEYS contains an invalid Fernet key.") from exc

    @classmethod
    def encrypt(cls, plaintext: str) -> str:
        """Encrypt ``plaintext`` with the current key and return the token as text."""
        return cls.cipher().encrypt(plaintext.encode()).decode()

    @classmethod
    def decrypt(cls, ciphertext: str) -> str:
        """Decrypt a value produced by ``encrypt`` with any configured key."""
        return cls.cipher().decrypt(ciphertext.encode()).decode()
