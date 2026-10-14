"""Generate a saved report from its prompt, outside the request."""

import logging

from celery import shared_task
from django.utils.translation import gettext as _

from apps.reports.constants import ReportGenerationStatus
from apps.reports.models import Report
from apps.reports.services.create_report import ReportCreationService
from company.active_company import ActiveCompany

logger = logging.getLogger(__name__)


@shared_task(name="reports.tasks.generate_report")
def generate_report(report_id: int):
    """Classify and fill a report that the admin view already saved."""

    try:
        report = Report.objects.select_related("owner", "company").prefetch_related(
            "included_companies"
        ).get(pk=report_id)
    except Report.DoesNotExist:
        logger.error("generate_report: report %s not found", report_id)
        return {
            "task": "generate_report",
            "report_id": report_id,
            "status": "error",
            "detail": "not found",
        }

    company_ids = [company.pk for company in report.included_companies.all()]
    if not company_ids and report.company_id:
        company_ids = [report.company_id]
    ActiveCompany.set_current_ids(company_ids or None)
    try:
        ReportCreationService().apply(report)
    except Exception:
        logger.exception("generate_report failed for report %s", report_id)
        report.status = ReportGenerationStatus.ERROR
        report.generation_error = _("Could not generate the report.")
        report.save(update_fields=["status", "generation_error", "updated_at"])
        return {
            "task": "generate_report",
            "report_id": report_id,
            "status": "error",
        }
    finally:
        ActiveCompany.clear_current()

    return {"task": "generate_report", "report_id": report_id, "status": "ok"}
