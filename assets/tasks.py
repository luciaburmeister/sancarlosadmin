import logging

from celery import shared_task

logger = logging.getLogger(__name__)


@shared_task(bind=True, max_retries=2, default_retry_delay=60)
def send_asset_planning_request_created_notification(self, request_id: int):
    """Notify a company's configured recipients (per service_type) that a new
    AssetPlanningRequest was created and needs review."""
    from django.conf import settings
    from django.template.loader import render_to_string
    from django.urls import reverse

    from assets.models import AssetPlanningNotificationRecipient, AssetPlanningRequest
    from core.services.email_service import EmailService

    try:
        req = AssetPlanningRequest.objects.select_related(
            "company", "to_zone", "from_zone", "requested_by"
        ).get(pk=request_id)
    except AssetPlanningRequest.DoesNotExist:
        logger.error("AssetPlanningRequest %s does not exist, skipping notification.", request_id)
        return {"status": "error", "message": f"AssetPlanningRequest {request_id} not found."}

    company = req.company
    recipients = list(
        AssetPlanningNotificationRecipient.objects.filter(
            company=company, service_type=req.service_type
        ).values_list("email", flat=True)
    ) if company else []
    if not recipients:
        return {"status": "skipped", "reason": "no_recipients"}

    admin_path = reverse("admin:assets_assetplanningrequest_change", args=[req.pk])
    request_url = f"{settings.BACKEND_URL.rstrip('/')}{admin_path}"

    try:
        requester_name = str(req.requested_by) if req.requested_by else ""
        html_message = render_to_string(
            "assets/email/asset_planning_request_created.html",
            {
                "company_name": company.name,
                "service_type": req.get_service_type_display(),
                "to_zone": req.to_zone,
                "from_zone": req.from_zone,
                "needed_date": req.needed_date,
                "needed_time": req.needed_time,
                "urgency": req.get_urgency_display(),
                "requester_name": requester_name,
                "notes": req.notes,
                "request_url": request_url,
            },
        )
        success = EmailService.send_email(
            to=recipients,
            subject=f"Nueva solicitud de planificación: {req.get_service_type_display()}",
            body_html=html_message,
            related_object=req,
            company=company,
        )
    except Exception as exc:
        logger.exception("Failed to send AssetPlanningRequest creation notification for %s", request_id)
        raise self.retry(exc=exc)

    return {"status": "ok" if success else "failed", "request_id": request_id}


@shared_task(bind=True, max_retries=2, default_retry_delay=60)
def send_asset_planning_draft_created_notification(self, draft_id: int):
    """Notify a company's configured emails that a new AssetPlanningRequestDraft
    was created from the mobile app."""
    from django.conf import settings
    from django.template.loader import render_to_string
    from django.urls import reverse

    from assets.models import AssetPlanningRequestDraft
    from core.services.email_service import EmailService

    try:
        draft = AssetPlanningRequestDraft.objects.select_related(
            "company", "requested_by", "client", "to_zone", "from_zone"
        ).get(pk=draft_id)
    except AssetPlanningRequestDraft.DoesNotExist:
        logger.error("AssetPlanningRequestDraft %s does not exist, skipping notification.", draft_id)
        return {"status": "error", "message": f"AssetPlanningRequestDraft {draft_id} not found."}

    company = draft.company
    recipients = company.asset_plan_draft_notification_emails if company else []
    if not recipients:
        return {"status": "skipped", "reason": "no_recipients"}

    admin_path = reverse("admin:assets_assetplanningrequestdraft_change", args=[draft.pk])
    draft_url = f"{settings.BACKEND_URL.rstrip('/')}{admin_path}"

    try:
        requester_name = str(draft.requested_by) if draft.requested_by else ""
        html_message = render_to_string(
            "assets/email/asset_planning_draft_created.html",
            {
                "company_name": company.name,
                "service_type": draft.get_service_type_display(),
                "requester_name": requester_name,
                "needed_date": draft.needed_date,
                "notes": draft.notes,
                "draft_url": draft_url,
            },
        )
        success = EmailService.send_email(
            to=recipients,
            subject=f"Nuevo borrador de planificación: {draft.get_service_type_display()}",
            body_html=html_message,
            related_object=draft,
            company=company,
        )
    except Exception as exc:
        logger.exception("Failed to send AssetPlanningRequestDraft creation notification for %s", draft_id)
        raise self.retry(exc=exc)

    return {"status": "ok" if success else "failed", "draft_id": draft_id}


@shared_task
def release_stale_cuba_reservations():
    """Clear reserved_asset on plans whose scheduled_date is before today."""
    from assets.services.cuba_reservation import release_stale_reservations

    released_ids = release_stale_reservations()
    return {"status": "ok", "released_planning_ids": released_ids}


@shared_task
def apply_due_zone_sync_requests():
    """Beat: apply approved planning_type == NONE requests (Machinist Movement,
    Machine Self-Transport) whose needed_date/needed_time has arrived — covers
    requests approved ahead of their effective moment (see
    AssetPlanningRequestAdmin._approve_zone_sync_request, which applies
    already-due approvals immediately instead of waiting for this task)."""
    from django.utils import timezone

    from assets.models import AssetPlanningRequest, AssetPlanningRequestStatus, PlanningType

    candidates = AssetPlanningRequest.objects.filter(
        planning_type=PlanningType.NONE,
        status=AssetPlanningRequestStatus.APPROVED,
        needed_date__lte=timezone.localdate(),
        zone_sync_applied_at__isnull=True,
    )
    applied_ids = [req.pk for req in candidates if req.is_zone_sync_due and req.apply_zone_change()]
    return {"status": "ok", "applied_request_ids": applied_ids}
