from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _


class AIConversation(models.Model):
    """A chat thread with the AI agent.

    Data questions can still span every company the user has active.
    ``claude_company`` only chooses whose Claude API key pays for the turn.
    """

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="ai_conversations",
        verbose_name=_("Owner"),
    )
    claude_company = models.ForeignKey(
        "company.Company",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="ai_conversations",
        verbose_name=_("Company"),
    )
    title = models.CharField(max_length=200, blank=True, verbose_name=_("Title"))
    state = models.JSONField(default=dict, blank=True, verbose_name=_("State"))
    created_at = models.DateTimeField(auto_now_add=True, verbose_name=_("Created at"))
    updated_at = models.DateTimeField(auto_now=True, verbose_name=_("Updated at"))

    class Meta:
        verbose_name = _("AI conversation")
        verbose_name_plural = _("AI conversations")
        ordering = ["-updated_at"]
        default_permissions = ()
        permissions = [
            ("use_agent", _("Can use the ERP AI agent")),
        ]

    def __str__(self) -> str:
        return self.title or _("Conversation #%(pk)s") % {"pk": self.pk}


class AIMessage(models.Model):
    class Role(models.TextChoices):
        USER = "user", _("User")
        ASSISTANT = "assistant", _("Assistant")

    conversation = models.ForeignKey(
        AIConversation,
        on_delete=models.CASCADE,
        related_name="messages",
        verbose_name=_("Conversation"),
    )
    role = models.CharField(max_length=16, choices=Role.choices, verbose_name=_("Role"))
    content = models.JSONField(default=list, verbose_name=_("Content"))
    display_text = models.TextField(blank=True, verbose_name=_("Display text"))
    tool_calls = models.JSONField(default=list, blank=True, verbose_name=_("Tool calls"))
    chart = models.JSONField(null=True, blank=True, verbose_name=_("Chart"))
    artifacts = models.JSONField(default=list, blank=True, verbose_name=_("Artifacts"))
    created_at = models.DateTimeField(auto_now_add=True, verbose_name=_("Created at"))

    class Meta:
        verbose_name = _("AI message")
        verbose_name_plural = _("AI messages")
        ordering = ["created_at", "pk"]
        default_permissions = ()

    def __str__(self) -> str:
        return f"{self.get_role_display()} @ {self.created_at:%Y-%m-%d %H:%M}"


class AIArtifactKind(models.TextChoices):
    PDF = "pdf", _("PDF")
    EXCEL = "xlsx", _("Excel")


class AIArtifact(models.Model):
    """A generated document (PDF/Excel) the agent produced for a chat."""

    conversation = models.ForeignKey(
        AIConversation,
        on_delete=models.CASCADE,
        related_name="artifacts",
        verbose_name=_("Conversation"),
    )
    kind = models.CharField(
        max_length=8, choices=AIArtifactKind.choices, verbose_name=_("Kind")
    )
    title = models.CharField(max_length=200, verbose_name=_("Title"))
    file = models.FileField(
        upload_to="ai_agent/artifacts/%Y/%m/", verbose_name=_("File")
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name=_("Created at"))

    class Meta:
        verbose_name = _("AI artifact")
        verbose_name_plural = _("AI artifacts")
        ordering = ["-created_at"]
        default_permissions = ()

    def __str__(self) -> str:
        return self.title
