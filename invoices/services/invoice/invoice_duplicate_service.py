"""
Invoice Duplicate Detection Service

Handles file hashing and duplicate detection for uploaded invoices.
Uses standard cryptographic hashing (SHA-256) to detect exactly identical files.
"""

import hashlib
from typing import Optional
from django.core.files.uploadedfile import UploadedFile
from invoices.models import Invoice


# ---------------------------------------------------------------------
# Result Container
# ---------------------------------------------------------------------

class DuplicateDetectionResult:
    """Container for duplicate detection results"""

    def __init__(
        self,
        is_duplicate: bool,
        similar_invoice: Optional[Invoice] = None,
        similarity_distance: Optional[int] = None,
        hash_value: Optional[str] = None,
    ):
        self.is_duplicate = is_duplicate
        self.similar_invoice = similar_invoice
        self.similarity_distance = similarity_distance
        self.hash_value = hash_value


class DuplicateInvoiceError(Exception):
    """Custom exception for duplicate invoice detection"""

    def __init__(self, similar_invoice: Invoice, distance: int):
        self.similar_invoice = similar_invoice
        self.distance = distance
        super().__init__(
            f"Duplicate invoice detected: similar to {similar_invoice} (distance: {distance})"
        )


# ---------------------------------------------------------------------
# Main Service
# ---------------------------------------------------------------------

class InvoiceDuplicateService:
    """
    Service for detecting duplicate invoices using SHA-256 cryptographic hashing.
    """

    def __init__(self, similarity_threshold: int = 0):
        # Kept parameter for backward compatibility, but threshold is essentially 0
        # for cryptographic hash matches.
        self.similarity_threshold = 0

    # ================================================================
    # PUBLIC METHODS
    # ================================================================

    def check_for_duplicate(self, file_path: str) -> DuplicateDetectionResult:
        """
        Check duplicate from a local file path (used in processing pipeline).
        """
        new_hash = self.calculate_hash_from_path(file_path)
        return self._compare_against_existing(new_hash)

    def check_for_duplicate_file(self, uploaded_file: UploadedFile) -> DuplicateDetectionResult:
        """
        Check duplicate from an uploaded Django file (used in upload form).
        """
        new_hash = self.calculate_hash_from_uploaded_file(uploaded_file)
        return self._compare_against_existing(new_hash)

    # ================================================================
    # HASH CALCULATION
    # ================================================================

    def calculate_hash_from_path(self, file_path: str) -> str:
        """
        Calculate SHA-256 hash from file path.
        """
        try:
            sha256_hash = hashlib.sha256()
            with open(file_path, "rb") as f:
                # Read and update hash string value in blocks of 4K
                for byte_block in iter(lambda: f.read(4096), b""):
                    sha256_hash.update(byte_block)
            return sha256_hash.hexdigest()
        except Exception as e:
            raise ValueError(f"Failed to calculate hash for {file_path}: {str(e)}")

    def calculate_hash_from_uploaded_file(self, uploaded_file: UploadedFile) -> str:
        """
        Calculate SHA-256 hash from Django UploadedFile.
        """
        try:
            sha256_hash = hashlib.sha256()
            uploaded_file.seek(0)
            for chunk in uploaded_file.chunks():
                sha256_hash.update(chunk)
            uploaded_file.seek(0)
            return sha256_hash.hexdigest()

        except Exception as e:
            import traceback
            traceback.print_exc()
            raise InvoiceHashError(str(e))

    # ================================================================
    # INTERNAL HELPERS
    # ================================================================

    def _compare_against_existing(self, new_hash: str) -> DuplicateDetectionResult:
        """
        Compare a newly calculated hash against stored invoice hashes.
        Uses exact string match in the database.
        """
        existing_invoice = Invoice.objects.filter(image_hash=new_hash).first()

        if existing_invoice:
            return DuplicateDetectionResult(
                is_duplicate=True,
                similar_invoice=existing_invoice,
                similarity_distance=0, # Cryptographic hash distance is 0 for exact match
                hash_value=new_hash,
            )

        return DuplicateDetectionResult(
            is_duplicate=False,
            hash_value=new_hash,
        )


class InvoiceHashError(Exception):
    pass
