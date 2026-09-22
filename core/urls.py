from django.urls import path

from core.views.email_event_webhook import EmailEventWebhookView


urlpatterns = [
    # The provider key is the URL: /api/email-events/ses/, and whatever a
    # second registered provider is called.
    path("<slug:provider>/", EmailEventWebhookView.as_view(), name="email_event_webhook"),
]
