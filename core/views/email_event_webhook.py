"""Public, signature-verified endpoint for provider email-event webhooks."""

import logging

from django.http import JsonResponse
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.csrf import csrf_exempt

from rest_framework.throttling import SimpleRateThrottle

from general.client_ip import client_ip
from general.email.providers import InvalidWebhook, get_provider

logger = logging.getLogger(__name__)


class RejectedWebhookThrottle(SimpleRateThrottle):
    """
    Throttles callers by how many of their requests we *rejected*.

    Genuine provider traffic is never throttled: it arrives in bursts after
    bulk sends, and a dropped event is lost data. So `allow_request` only
    checks, and `record_rejection` is what counts.
    """

    scope = "email_webhook_rejections"
    rate = "100/hour"

    def get_ident(self, request):
        return client_ip(request)

    def get_cache_key(self, request, view):
        return self.cache_format % {"scope": self.scope, "ident": self.get_ident(request)}

    def throttle_success(self):
        return True

    def record_rejection(self):
        super().throttle_success()


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

        throttle = RejectedWebhookThrottle()
        if not throttle.allow_request(request, self):
            return JsonResponse({"error": "Too many invalid requests"}, status=429)

        try:
            payload, status = declaration.handle_webhook(request)
        except InvalidWebhook:
            throttle.record_rejection()
            logger.warning("Rejected invalid %s email event", provider)
            return JsonResponse({"error": "Invalid notification"}, status=403)

        return JsonResponse(payload, status=status)
