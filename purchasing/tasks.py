from celery import shared_task
from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string
from django.urls import reverse
import logging

logger = logging.getLogger(__name__)


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
def send_provider_quote_email(provider_quote_id: int, language: str = "es"):
    """
    Emails a provider the public link to fill in their ProviderQuote,
    then marks it as sent. Runs through Celery so the admin request
    doesn't have to wait on the SMTP round trip.

    `language` picks which of the two HTML templates (and subject/copy)
    to use — "es" or "en". Falls back to "es" for anything else.

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
