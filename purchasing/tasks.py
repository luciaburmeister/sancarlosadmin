"""Celery tasks for the purchasing app."""
import logging

from celery import shared_task

logger = logging.getLogger(__name__)


@shared_task(bind=True, max_retries=2, default_retry_delay=60)
def send_purchase_order_created_notification(self, purchase_order_id: int):
    """Notify a company's configured recipients that a new Purchase Order was created.

    Skipped when the company has no recipients configured
    (Company.purchase_order_notification_emails) — opt-in per company.
    """
    from django.conf import settings
    from django.template.loader import render_to_string
    from django.urls import reverse

    from company.models import Company
    from core.services.email_service import EmailService
    from purchasing.models import PurchaseOrder

    try:
        po = PurchaseOrder.objects.select_related("company", "provider").get(pk=purchase_order_id)
    except PurchaseOrder.DoesNotExist:
        logger.error("PurchaseOrder %s does not exist, skipping notification.", purchase_order_id)
        return {"status": "error", "message": f"PurchaseOrder {purchase_order_id} not found."}

    company = po.company
    recipients = company.purchase_order_notification_emails if company else []
    if not recipients:
        return {"status": "skipped", "reason": "no_recipients"}

    admin_path = reverse("admin:purchasing_purchaseorder_change", args=[po.pk])
    po_url = f"{settings.BACKEND_URL.rstrip('/')}{admin_path}"

    try:
        html_message = render_to_string(
            "purchasing/email/purchase_order_created.html",
            {
                "company_name": company.name,
                "po_number": po.number,
                "provider_name": po.provider.name,
                "total_amount": po.total_amount,
                "date_ordered": po.date_ordered,
                "project_file": po.project_file,
                "po_url": po_url,
            },
        )
        success = EmailService.send_email(
            to=recipients,
            subject=f"Nueva orden de compra: {po.number}",
            body_html=html_message,
            related_object=po,
            company=company,
        )
    except Exception as exc:
        logger.exception("Failed to send PO creation notification for PO %s", purchase_order_id)
        raise self.retry(exc=exc)

    return {"status": "ok" if success else "failed", "purchase_order_id": purchase_order_id}


@shared_task(bind=True, max_retries=2, default_retry_delay=60)
def send_purchase_order_decision_notification(self, purchase_order_id: int, decision: str):
    """Notify the user who requested a Purchase Order that it was approved or rejected.

    Skipped when the PO has no requester or the requester has no email address.
    """
    from django.conf import settings
    from django.template.loader import render_to_string
    from django.urls import reverse

    from core.services.email_service import EmailService
    from purchasing.models import PurchaseOrder

    try:
        po = PurchaseOrder.objects.select_related(
            "company", "provider", "requested_by", "approved_by", "rejected_by"
        ).get(pk=purchase_order_id)
    except PurchaseOrder.DoesNotExist:
        logger.error("PurchaseOrder %s does not exist, skipping decision notification.", purchase_order_id)
        return {"status": "error", "message": f"PurchaseOrder {purchase_order_id} not found."}

    requester = po.requested_by
    if not requester or not requester.email:
        return {"status": "skipped", "reason": "no_recipient"}

    approved = decision == "approved"
    reviewer = po.approved_by if approved else po.rejected_by
    reviewer_name = (reviewer.get_full_name() or reviewer.username) if reviewer else ""

    admin_path = reverse("admin:purchasing_purchaseorder_change", args=[po.pk])
    po_url = f"{settings.BACKEND_URL.rstrip('/')}{admin_path}"

    try:
        html_message = render_to_string(
            "purchasing/email/purchase_order_decision.html",
            {
                "approved": approved,
                "po_number": po.number,
                "provider_name": po.provider.name,
                "total_amount": po.total_amount,
                "project_file": po.project_file,
                "reviewer_name": reviewer_name,
                "rejection_reason": po.rejection_reason,
                "po_url": po_url,
            },
        )
        decision_label = "Aprobada" if approved else "Rechazada"
        success = EmailService.send_email(
            to=[requester.email],
            subject=f"Orden de compra {po.number}: {decision_label}",
            body_html=html_message,
            related_object=po,
            company=po.company,
        )
    except Exception as exc:
        logger.exception("Failed to send PO decision notification for PO %s", purchase_order_id)
        raise self.retry(exc=exc)

    return {"status": "ok" if success else "failed", "purchase_order_id": purchase_order_id}
