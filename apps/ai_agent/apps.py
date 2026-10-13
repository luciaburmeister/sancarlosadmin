from django.apps import AppConfig


class AiAgentConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.ai_agent"
    label = "ai_agent"
    verbose_name = "AI Agent"

    def ready(self):
        from apps.ai_agent.registry import ToolRegistry

        ToolRegistry.autodiscover()
