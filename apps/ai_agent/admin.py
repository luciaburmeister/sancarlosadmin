"""AI agent admin integration.

Registers the chat page on ``AIConversationAdmin`` itself via ``get_urls()``
(one page, not a changelist — same approach the old chatbot app used),
plus a read-only changelist over AIArtifact for audit.
"""

from __future__ import annotations

from django.contrib import admin
from django.urls import path
from unfold.admin import ModelAdmin

from apps.ai_agent.models import AIArtifact, AIConversation, AIMessage
from apps.ai_agent.views import ChatPageView


class AIMessageInline(admin.TabularInline):
    model = AIMessage
    extra = 0
    fields = ("role", "display_text", "tool_calls", "created_at")
    readonly_fields = fields
    can_delete = False
    show_change_link = False


@admin.register(AIConversation)
class AIConversationAdmin(ModelAdmin):
    """Read-only audit trail — conversations are created/used via the chat page."""

    list_display = ("title_display", "owner", "updated_at", "message_count")
    list_filter = ("owner",)
    readonly_fields = ("owner", "title", "state", "created_at", "updated_at")
    fields = readonly_fields
    inlines = [AIMessageInline]

    def get_urls(self):
        return [
            path(
                "chat/",
                self.admin_site.admin_view(ChatPageView.as_view()),
                name="ai_agent_chat",
            ),
        ] + super().get_urls()

    def has_module_permission(self, request):
        return request.user.has_perm("ai_agent.use_agent")

    def has_view_permission(self, request, obj=None):
        return request.user.has_perm("ai_agent.use_agent")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return request.user.has_perm("ai_agent.use_agent")

    def has_delete_permission(self, request, obj=None):
        return request.user.is_superuser

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        if request.user.is_superuser:
            return qs
        return qs.filter(owner=request.user)

    def title_display(self, obj):
        return obj.title or f"Conversation #{obj.pk}"

    def message_count(self, obj):
        return obj.messages.count()


@admin.register(AIArtifact)
class AIArtifactAdmin(ModelAdmin):
    list_display = ("title", "kind", "conversation", "created_at")
    list_filter = ("kind",)
    readonly_fields = ("conversation", "kind", "title", "file", "created_at")
    fields = readonly_fields

    def has_module_permission(self, request):
        return request.user.has_perm("ai_agent.use_agent")

    def has_view_permission(self, request, obj=None):
        return request.user.has_perm("ai_agent.use_agent")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return request.user.is_superuser

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        if request.user.is_superuser:
            return qs
        return qs.filter(conversation__owner=request.user)
