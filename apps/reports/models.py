from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _

from company.models import CompanyOwnedModel

from apps.reports.constants import (
    ReportDomain,
    ReportGenerationStatus,
    ReportVisibility,
)


class Report(CompanyOwnedModel):
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="owned_reports",
        verbose_name=_("Owner"),
    )
    name = models.CharField(max_length=200, verbose_name=_("Name"))
    prompt = models.TextField(verbose_name=_("Original prompt"))
    domain = models.CharField(
        max_length=32,
        choices=ReportDomain.choices,
        blank=True,
        verbose_name=_("Domain"),
    )
    spec = models.JSONField(default=dict, blank=True, verbose_name=_("Report spec"))
    status = models.CharField(
        max_length=16,
        choices=ReportGenerationStatus.choices,
        default=ReportGenerationStatus.READY,
        verbose_name=_("Status"),
    )
    generation_summary = models.TextField(
        blank=True, verbose_name=_("Generation summary")
    )
    generation_error = models.TextField(blank=True, verbose_name=_("Generation error"))
    generation_suggestions = models.JSONField(
        default=list,
        blank=True,
        verbose_name=_("Prompt suggestions"),
    )
    visibility = models.CharField(
        max_length=16,
        choices=ReportVisibility.choices,
        default=ReportVisibility.PRIVATE,
        verbose_name=_("Visibility"),
    )
    included_companies = models.ManyToManyField(
        "company.Company",
        blank=True,
        related_name="included_in_reports",
        verbose_name=_("Included companies"),
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name=_("Created at"))
    updated_at = models.DateTimeField(auto_now=True, verbose_name=_("Updated at"))

    class Meta:
        verbose_name = _("Report")
        verbose_name_plural = _("Reports")
        ordering = ["-updated_at"]
        default_permissions = ()
        permissions = [
            ("use_reports", _("Can use natural language reports")),
        ]

    def __str__(self):
        return self.name
