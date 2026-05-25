from django.apps import AppConfig


class ChatbotConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "chatbot"
    verbose_name = "ERP Chatbot"

    def ready(self):
        from chatbot.registry import ToolRegistry
        ToolRegistry.autodiscover()
