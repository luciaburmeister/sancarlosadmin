from celery import shared_task
from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string
from django.urls import reverse
from django.db import transaction
from django.utils import timezone
import logging

from general.email import resolve_reply_to

logger = logging.getLogger(__name__)


@shared_task(name="purchasing.tasks.process_due_purchase_order_approvals")
def process_due_purchase_order_approvals():
    """Approve eligible project orders once their persisted delay has elapsed."""
    from management.models import ProjectFile
    from purchasing.models import ProjectPurchaseApprovalRule, PurchaseOrder, PurchaseOrderStatus

    due_at = timezone.now()
    due_ids = list(
        PurchaseOrder.objects.filter(
            status=PurchaseOrderStatus.PENDING_APPROVAL,
            automatic_approval_due_at__lte=due_at,
        ).order_by("automatic_approval_due_at", "pk").values_list("pk", flat=True)[:100]
    )
    approved = 0
    for purchase_order_id in due_ids:
        with transaction.atomic():
            po = PurchaseOrder.objects.select_for_update(skip_locked=True).filter(
                pk=purchase_order_id,
                status=PurchaseOrderStatus.PENDING_APPROVAL,
                automatic_approval_due_at__lte=due_at,
            ).first()
            if po is None:
                continue

            rule = ProjectPurchaseApprovalRule.objects.filter(
                company_id=po.company_id, is_active=True
            ).first()
            if rule and po.project_file_id:
                projects = ProjectFile.objects.filter(
                    pk=po.project_file_id, company_id=po.company_id
                )
                if rule.minimum_margin_percent is not None:
                    projects = projects.select_for_update()
                project = projects.first()
                if project is None:
                    rule = None
                else:
                    po.project_file = project

            po.automatic_approval_due_at = None
            if rule is None or not rule.approves(po):
                po.save(update_fields=["automatic_approval_due_at"])
                continue

            po.status = PurchaseOrderStatus.APPROVED
            po.approved_at = timezone.now()
            po.approved_automatically = True
            po.save(update_fields=[
                "status", "approved_at", "approved_automatically", "automatic_approval_due_at"
            ])
            transaction.on_commit(
                lambda po_id=po.pk: send_purchase_order_decision_notification.delay(po_id, "approved")
            )
            approved += 1
    return {"processed": len(due_ids), "approved": approved}


EMAIL_COPY = {
    "en": {
        "template": "purchasing/email/provider_quote_request_en.html",
        "subject": "Quote Request from {company}",
        "greeting": "Hello {provider},",
        "intro": "{company} would like to request a quote from you.",
        "cta": "Please use the link below to review the request and submit your pricing:",
        "closing": "If you have any questions, feel free to reach out.\n\nThank you,\n{company}",
    },
    "es": {
        "template": "purchasing/email/provider_quote_request_es.html",
        "subject": "Solicitud de Presupuesto de {company}",
        "greeting": "Estimado/a {provider}:",
        "intro": "{company} le solicita un presupuesto.",
        "cta": "Utilice el siguiente enlace para revisar la solicitud y completar su presupuesto:",
        "closing": "Si tiene alguna consulta, no dude en contactar con nosotros.\n\nUn cordial saludo,\n{company}",
    },
}


@shared_task(name="purchasing.tasks.send_provider_quote_email")
def send_provider_quote_email(
    provider_quote_id: int,
    language: str = "es",
    reply_to: list[str] | None = None,
):
    """
    Emails a provider the public link to fill in their ProviderQuote,
    then marks it as sent. Runs through Celery so the admin request
    doesn't have to wait on the SMTP round trip.

    `language` picks which of the two HTML templates (and subject/copy)
    to use — "es" or "en". Falls back to "es" for anything else.

    `reply_to` is the sending user's own address, so the provider replies to
    the person who asked them for a quote rather than to the shared mailbox.
    Falls back to the company's configured Reply-To when absent.

    Can be triggered:
    - Manually:   send_provider_quote_email.delay(provider_quote_id, "en")
    """
    from purchasing.models import ProviderQuote

    logger.info(f"Starting send_provider_quote_email for ProviderQuote {provider_quote_id} ({language})")

    language = language if language in EMAIL_COPY else "es"
    copy = EMAIL_COPY[language]

    try:
        quote = (
            ProviderQuote.objects
            .select_related("provider", "quote_request__company")
            .get(pk=provider_quote_id)
        )
    except ProviderQuote.DoesNotExist:
        logger.error(f"send_provider_quote_email: ProviderQuote {provider_quote_id} not found")
        return {"task": "send_provider_quote_email", "status": "error", "detail": "quote not found"}

    if not quote.provider.email:
        logger.error(f"send_provider_quote_email: provider {quote.provider_id} has no email on file")
        return {"task": "send_provider_quote_email", "status": "error", "detail": "provider has no email"}

    link_path = reverse("purchasing_quote_public_response", args=[quote.token])
    link_url = f"{settings.BACKEND_URL.rstrip('/')}{link_path}"

    company_name = quote.quote_request.company.name if quote.quote_request.company else "Polipus"

    subject = copy["subject"].format(company=company_name)

    html_body = render_to_string(copy["template"], {
        "provider_name": quote.provider.name,
        "company_name": company_name,
        "link_url": link_url,
    })

    text_body = (
        f"{copy['greeting'].format(provider=quote.provider.name)}\n\n"
        f"{copy['intro'].format(company=company_name)}\n\n"
        f"{copy['cta']}\n\n"
        f"{link_url}\n\n"
        f"{copy['closing'].format(company=company_name)}"
    )

    try:
        email = EmailMultiAlternatives(
            subject=subject,
            body=text_body,
            from_email=f"{company_name} <{settings.DEFAULT_FROM_EMAIL}>",
            to=[quote.provider.email],
            reply_to=reply_to or resolve_reply_to(
                company=quote.quote_request.company, email_document="provider_quote"
            ),
        )
        email.attach_alternative(html_body, "text/html")
        email.send()
    except Exception as e:
        logger.error(f"send_provider_quote_email failed for quote {provider_quote_id}: {str(e)}")
        raise

    quote.mark_as_sent()
    logger.info(f"send_provider_quote_email: sent quote {quote.code} to {quote.provider.email} ({language})")

    return {
        "task": "send_provider_quote_email",
        "status": "ok",
        "quote": quote.code,
        "to": quote.provider.email,
        "language": language,
    }


@shared_task(bind=True, max_retries=2, default_retry_delay=60)
def send_purchase_order_pending_approval_notification(self, purchase_order_id: int):
    """Notify a company's configured approvers that a Purchase Order awaits approval.

    Sent when a PO is submitted for approval. Skipped when the company has no
    recipients configured (Company.purchase_order_notification_emails) — opt-in
    per company.
    """
    from django.conf import settings
    from django.template.loader import render_to_string
    from django.urls import reverse

    from company.models import Company
    from core.services.email_service import EmailService
    from purchasing.models import PurchaseOrder, PurchaseOrderStatus

    try:
        po = PurchaseOrder.objects.select_related("company", "provider", "requested_by").get(pk=purchase_order_id)
    except PurchaseOrder.DoesNotExist:
        logger.error("PurchaseOrder %s does not exist, skipping notification.", purchase_order_id)
        return {"status": "error", "message": f"PurchaseOrder {purchase_order_id} not found."}

    if po.status != PurchaseOrderStatus.PENDING_APPROVAL:
        return {"status": "skipped", "reason": "not_pending"}

    company = po.company
    recipients = company.purchase_order_notification_emails if company else []
    if not recipients:
        return {"status": "skipped", "reason": "no_recipients"}

    admin_path = reverse("admin:purchasing_purchaseorder_change", args=[po.pk])
    po_url = f"{settings.BACKEND_URL.rstrip('/')}{admin_path}"

    try:
        requester = po.requested_by
        requester_name = (requester.get_full_name() or requester.username) if requester else ""
        html_message = render_to_string(
            "purchasing/email/purchase_order_pending_approval.html",
            {
                "company_name": company.name,
                "po_number": po.number,
                "provider_name": po.provider.name,
                "total_amount": po.total_amount,
                "date_ordered": po.date_ordered,
                "project_file": po.project_file,
                "requester_name": requester_name,
                "is_immediate_bank_transfer": po.is_immediate_bank_transfer,
                "po_url": po_url,
            },
        )
        success = EmailService.send_email(
            to=recipients,
            subject=f"Orden de compra pendiente de aprobación: {po.number}",
            body_html=html_message,
            related_object=po,
            company=company,
            email_document="purchase_order_approval",
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
                "approved_automatically": po.approved_automatically,
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
            email_document="purchase_order_decision",
        )
    except Exception as exc:
        logger.exception("Failed to send PO decision notification for PO %s", purchase_order_id)
        raise self.retry(exc=exc)

    return {"status": "ok" if success else "failed", "purchase_order_id": purchase_order_id}


@shared_task(bind=True, max_retries=2, default_retry_delay=60)
def send_purchase_order_message_notification(self, message_id: int):
    """Notify the recipient of a Purchase Order conversation message.

    The recipient is resolved when the message is created. Messages with no
    recipient (the requester opened the thread before anyone asked) go to the
    company's approver list instead; when that list is empty too, nothing is
    sent. Replies are routed back to the sender.
    """
    from django.conf import settings
    from django.template.loader import render_to_string
    from django.urls import reverse

    from core.services.email_service import EmailService
    from purchasing.models import PurchaseOrderMessage

    try:
        message = PurchaseOrderMessage.objects.select_related(
            "sender", "recipient", "purchase_order__company", "purchase_order__provider"
        ).get(pk=message_id)
    except PurchaseOrderMessage.DoesNotExist:
        logger.error("PurchaseOrderMessage %s does not exist, skipping notification.", message_id)
        return {"status": "error", "message": f"PurchaseOrderMessage {message_id} not found."}

    po = message.purchase_order
    company = po.company

    if message.recipient:
        if not message.recipient.email:
            return {"status": "skipped", "reason": "recipient_without_email"}
        recipients = [message.recipient.email]
    else:
        recipients = company.purchase_order_notification_emails if company else []
    if not recipients:
        return {"status": "skipped", "reason": "no_recipients"}

    sender = message.sender
    sender_name = (sender.get_full_name() or sender.username) if sender else ""

    admin_path = reverse("admin:purchasing_purchaseorder_change", args=[po.pk])
    po_url = f"{settings.BACKEND_URL.rstrip('/')}{admin_path}#messages"

    # The conversation so far, so the recipient can answer from the email
    # without opening the order. Only count the remainder when the cap was
    # actually reached.
    earlier = message.earlier_in_thread()
    history = list(earlier[: PurchaseOrderMessage.EMAIL_HISTORY_LIMIT])
    older_count = (
        earlier.count() - len(history)
        if len(history) == PurchaseOrderMessage.EMAIL_HISTORY_LIMIT
        else 0
    )

    try:
        html_message = render_to_string(
            "purchasing/email/purchase_order_message.html",
            {
                "sender_name": sender_name,
                "body": message.body,
                "history": history,
                "older_count": older_count,
                "excerpt_chars": PurchaseOrderMessage.EMAIL_HISTORY_EXCERPT_CHARS,
                "po_number": po.number,
                "provider_name": po.provider.name,
                "total_amount": po.total_amount,
                "project_file": po.project_file,
                "po_url": po_url,
            },
        )
        success = EmailService.send_email(
            to=recipients,
            subject=f"Orden de compra {po.number}: nuevo mensaje",
            body_html=html_message,
            related_object=po,
            company=company,
            email_document="purchase_order_message",
            reply_to=[sender.email] if sender and sender.email else None,
        )
    except Exception as exc:
        logger.exception("Failed to send PO message notification for message %s", message_id)
        raise self.retry(exc=exc)

    return {"status": "ok" if success else "failed", "message_id": message_id}
