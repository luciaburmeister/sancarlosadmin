"""
Symmetric encryption helpers for storing sensitive values (API keys) in the DB.

Uses Fernet (AES-128-CBC + HMAC-SHA256) with a key derived from Django's SECRET_KEY.
"""
import base64
import hashlib

from cryptography.fernet import Fernet
from django.conf import settings


def _get_fernet() -> Fernet:
    """Derive a Fernet key from SECRET_KEY (deterministic, no extra env var needed)."""
    digest = hashlib.sha256(settings.SECRET_KEY.encode()).digest()
    key = base64.urlsafe_b64encode(digest)
    return Fernet(key)


def encrypt_value(plaintext: str) -> str:
    """Encrypt a plaintext string and return a base64-encoded ciphertext."""
    return _get_fernet().encrypt(plaintext.encode()).decode()


def decrypt_value(ciphertext: str) -> str:
    """Decrypt a base64-encoded ciphertext and return the plaintext string."""
    return _get_fernet().decrypt(ciphertext.encode()).decode()
