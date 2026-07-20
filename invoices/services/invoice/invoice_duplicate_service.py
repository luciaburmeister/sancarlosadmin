"""
Invoice Duplicate Detection Service

Handles file hashing and duplicate detection for uploaded invoices.
Uses standard cryptographic hashing (SHA-256) to detect exactly identical files.
"""

from invoices.models import Invoice
from core.services.file_duplicate_service import FileDuplicateService


class InvoiceHashError(Exception):
    pass


class DuplicateInvoiceError(Exception):
    """Custom exception for duplicate invoice detection"""

    def __init__(self, similar_invoice: Invoice, distance: int):
        self.similar_invoice = similar_invoice
        self.distance = distance
        super().__init__(
            f"Duplicate invoice detected: similar to {similar_invoice} (distance: {distance})"
        )


class InvoiceDuplicateService(FileDuplicateService):
    """
    Service for detecting duplicate invoices using SHA-256 cryptographic hashing.
    """

    model = Invoice
    hash_field = "image_hash"
    hash_error_class = InvoiceHashError

    def __init__(self, similarity_threshold: int = 0):
        # Kept parameter for backward compatibility, but threshold is essentially 0
        # for cryptographic hash matches.
        self.similarity_threshold = 0
