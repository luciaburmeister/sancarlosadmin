"""
Base for detecting duplicate uploaded files via SHA-256 hashing.

Subclasses set `model`, `hash_field`, and `hash_error_class` to plug in
their own Django model, the field that stores the hash, and the exception
type to raise if hashing fails.
"""

import hashlib
from typing import Optional

from django.core.files.uploadedfile import UploadedFile


class DuplicateDetectionResult:
    def __init__(
        self,
        is_duplicate: bool,
        similar_object=None,
        hash_value: Optional[str] = None,
        similarity_distance: Optional[int] = None,
    ):
        self.is_duplicate = is_duplicate
        self.similar_object = similar_object
        self.hash_value = hash_value
        self.similarity_distance = similarity_distance


class FileDuplicateService:
    model = None
    hash_field = "file_hash"
    hash_error_class = Exception

    def check_for_duplicate_file(self, uploaded_file: UploadedFile, **filters) -> DuplicateDetectionResult:
        new_hash = self.calculate_hash_from_uploaded_file(uploaded_file)
        return self._compare_against_existing(new_hash, filters)

    def check_for_duplicate(self, file_path: str, **filters) -> DuplicateDetectionResult:
        new_hash = self.calculate_hash_from_path(file_path)
        return self._compare_against_existing(new_hash, filters)

    def calculate_hash_from_uploaded_file(self, uploaded_file: UploadedFile) -> str:
        try:
            sha256_hash = hashlib.sha256()
            uploaded_file.seek(0)
            for chunk in uploaded_file.chunks():
                sha256_hash.update(chunk)
            uploaded_file.seek(0)
            return sha256_hash.hexdigest()
        except Exception as e:
            raise self.hash_error_class(str(e))

    def calculate_hash_from_path(self, file_path: str) -> str:
        try:
            sha256_hash = hashlib.sha256()
            with open(file_path, "rb") as f:
                for byte_block in iter(lambda: f.read(4096), b""):
                    sha256_hash.update(byte_block)
            return sha256_hash.hexdigest()
        except Exception as e:
            raise self.hash_error_class(str(e))

    def _compare_against_existing(self, new_hash: str, filters: dict) -> DuplicateDetectionResult:
        existing = self.model.objects.filter(**{self.hash_field: new_hash}, **filters).first()

        if existing:
            return DuplicateDetectionResult(
                is_duplicate=True,
                similar_object=existing,
                hash_value=new_hash,
                similarity_distance=0,
            )

        return DuplicateDetectionResult(is_duplicate=False, hash_value=new_hash)
