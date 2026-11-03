"""Accepting a quote: it is filed under an expediente and marked accepted."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.db import transaction

from management.models import ProjectFile
from sales.models import QuoteStatus

if TYPE_CHECKING:
    from django.contrib.auth.models import AbstractBaseUser

    from sales.models import Quote


class QuoteAcceptanceService:
    """The one way a quote is accepted, by staff in the admin wizard or by the client online."""

    @classmethod
    @transaction.atomic
    def accept(
        cls,
        quote: Quote,
        project_file: ProjectFile | None = None,
        project_name: str = "",
        created_by: AbstractBaseUser | None = None,
    ) -> ProjectFile:
        """File the quote under ``project_file``, or a new expediente, and mark it accepted."""
        if project_file is None:
            project_file = cls.open_project_file(quote, project_name, created_by)
        quote.project_file = project_file
        quote.status = QuoteStatus.ACCEPTED
        quote.save()
        return project_file

    @staticmethod
    def open_project_file(
        quote: Quote, name: str = "", created_by: AbstractBaseUser | None = None
    ) -> ProjectFile:
        """A new expediente for the quote's client, named ``name`` or after the quote."""
        return ProjectFile.objects.create(
            name=(name or quote.project_name or f"Expediente - {quote.pk}")[:255],
            client=quote.client,
            company=quote.company,
            created_by=created_by,
        )
