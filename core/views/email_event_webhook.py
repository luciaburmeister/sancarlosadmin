"""Public, signature-verified endpoint for provider email-event webhooks."""

import logging

from django.http import JsonResponse
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.csrf import csrf_exempt

from general.email.providers import InvalidWebhook, get_provider

logger = logging.getLogger(__name__)


@method_decorator(csrf_exempt, name="dispatch")
class EmailEventWebhookView(View):
    """
    One URL per registered provider, e.g. `/api/email-events/ses/`.

    Authenticating the request is the provider's job: what counts as a genuine
    webhook differs completely between services, and the check is the only
    thing guarding this endpoint, which is public and unauthenticated.
    """

    def post(self, request, provider):
        declaration = get_provider(provider)
        if declaration is None:
            return JsonResponse({"error": "Unknown email provider"}, status=404)

        try:
            payload, status = declaration.handle_webhook(request)
        except InvalidWebhook:
            logger.warning("Rejected invalid %s email event", provider)
            return JsonResponse({"error": "Invalid notification"}, status=403)

        return JsonResponse(payload, status=status)
