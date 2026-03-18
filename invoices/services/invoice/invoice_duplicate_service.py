"""
Invoice Duplicate Detection Service

Handles perceptual hashing and duplicate detection for uploaded invoices.
Supports both image files and PDFs (first page rendered to image).

Uses perceptual hashing (pHash) to detect visually similar invoices.
"""

import imagehash
from PIL import Image
from typing import Optional
from io import BytesIO
from django.db.models import Q
from django.core.files.uploadedfile import UploadedFile
from invoices.models import Invoice
# PDF support
try:
    from pdf2image import convert_from_path, convert_from_bytes
    PDF_SUPPORT = True
except ImportError:
    PDF_SUPPORT = False


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
    Service for detecting duplicate invoices using perceptual hashing.

    similarity_threshold:
        Maximum Hamming distance to consider duplicates.
        Lower = stricter.
        Recommended: 5–10 for invoices.
    """

    def __init__(self, similarity_threshold: int = 5):
        self.similarity_threshold = similarity_threshold

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
        Calculate perceptual hash from file path.
        Supports images and PDFs.
        """
        try:
            image = self._load_image_from_path(file_path)
            hash_value = imagehash.phash(image, hash_size=8)
            return str(hash_value)
        except Exception as e:
            raise ValueError(f"Failed to calculate hash for {file_path}: {str(e)}")

    def calculate_hash_from_uploaded_file(self, uploaded_file: UploadedFile) -> str:
        """
        Calculate perceptual hash from Django UploadedFile.
        Supports images and PDFs.
        """
        try:
            uploaded_file.seek(0)

            image = self._load_image_from_uploaded_file(uploaded_file)

            hash_value = imagehash.phash(image, hash_size=8)

            uploaded_file.seek(0)
            return str(hash_value)

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
        """

        existing_invoices = Invoice.objects.exclude(
            Q(image_hash__isnull=True) | Q(image_hash="")
        )

        new_hash_obj = imagehash.hex_to_hash(new_hash)

        for invoice in existing_invoices:
            try:
                existing_hash = imagehash.hex_to_hash(invoice.image_hash)
                distance = existing_hash - new_hash_obj

                if distance <= self.similarity_threshold:
                    return DuplicateDetectionResult(
                        is_duplicate=True,
                        similar_invoice=invoice,
                        similarity_distance=distance,
                        hash_value=new_hash,
                    )

            except Exception:
                # Skip invalid hashes
                continue

        return DuplicateDetectionResult(
            is_duplicate=False,
            hash_value=new_hash,
        )

    # ================================================================
    # IMAGE LOADING
    # ================================================================

    def _load_image_from_path(self, file_path: str) -> Image.Image:
        """
        Load image from local file path.
        If PDF → convert first page to image.
        """

        if file_path.lower().endswith(".pdf"):
            if not PDF_SUPPORT:
                raise RuntimeError(
                    "pdf2image is required for PDF hashing. Install it first."
                )

            pages = convert_from_path(file_path, first_page=1, last_page=1)
            image = pages[0]

        else:
            image = Image.open(file_path)

        if image.mode != "RGB":
            image = image.convert("RGB")

        return image

    def _load_image_from_uploaded_file(self, uploaded_file: UploadedFile) -> Image.Image:
        """
        Load image from UploadedFile.
        If PDF → convert first page to image.
        """

        filename = uploaded_file.name.lower()

        if filename.endswith(".pdf"):
            if not PDF_SUPPORT:
                raise RuntimeError(
                    "pdf2image is required for PDF hashing. Install it first."
                )

            file_bytes = uploaded_file.read()
            pages = convert_from_bytes(file_bytes, first_page=1, last_page=1)
            image = pages[0]

        else:
            image = Image.open(uploaded_file)

        if image.mode != "RGB":
            image = image.convert("RGB")

        return image

class InvoiceHashError(Exception):
    pass
