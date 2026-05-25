"""
Chatbot admin integration — registers the chat view in the Unfold admin.
"""

from django.contrib import admin
from django.template.response import TemplateResponse
from django.urls import path


class ChatbotAdminView(admin.ModelAdmin):
    """Pseudo-admin that registers the chatbot page under the admin site."""

    def get_urls(self):
        return [
            path(
                "chat/",
                self.admin_site.admin_view(self.chat_view),
                name="chatbot_chat",
            ),
        ]

    def chat_view(self, request):
        context = {
            **self.admin_site.each_context(request),
            "title": "ERP Chatbot",
        }
        return TemplateResponse(request, "chatbot/chat.html", context)


# We register it on the default admin site as a custom admin view
# accessible at /admin/chatbot/chat/
class ChatbotAdminSite:
    """Registers chatbot URLs directly on the admin site."""

    @staticmethod
    def get_urls():
        return [
            path(
                "chatbot/chat/",
                admin.site.admin_view(ChatbotAdminSite.chat_view),
                name="chatbot_chat",
            ),
        ]

    @staticmethod
    def chat_view(request):
        context = {
            **admin.site.each_context(request),
            "title": "ERP Chatbot",
        }
        return TemplateResponse(request, "chatbot/chat.html", context)
