"""AI agent views — admin-authenticated chat API + artifact downloads.

The chat endpoints are plain JSON APIs (not form-processing), so a bare
``View`` is the right base here — same justification the chatbot app used
for this exact shape before it was retired in favor of this app.
"""

from __future__ import annotations

import json
import logging

from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied
from django.http import (
    FileResponse,
    Http404,
    JsonResponse,
    StreamingHttpResponse,
)
from django.shortcuts import get_object_or_404
from django.urls import reverse
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.csrf import csrf_protect
from django.views.generic import TemplateView
from django.utils.translation import gettext_lazy as _

from apps.ai_agent.models import AIArtifact, AIConversation, AIMessage
from apps.ai_agent.orchestrator import (
    ConversationService,
    companies_with_claude_key,
    resolve_claude_company,
)
from apps.ai_agent.save_report import SaveReportError, save_report_from_message
from apps.reports.constants import ReportVisibility
from company.active_company import ActiveCompany

logger = logging.getLogger(__name__)


def _can_use_agent(user) -> bool:
    return user.is_authenticated and user.is_staff and user.has_perm("ai_agent.use_agent")


class AgentRequiredJSONMixin(LoginRequiredMixin):
    """Restrict a JSON API view to staff with the ``ai_agent.use_agent`` permission."""

    def dispatch(self, request, *args, **kwargs):
        if not _can_use_agent(request.user):
            return JsonResponse({"error": str(_("AI agent access denied."))}, status=403)
        return super().dispatch(request, *args, **kwargs)


class AgentRequiredPageMixin(LoginRequiredMixin):
    """Restrict an HTML page view to staff with the ``ai_agent.use_agent`` permission."""

    def dispatch(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return super().dispatch(request, *args, **kwargs)
        if not _can_use_agent(request.user):
            raise PermissionDenied
        return super().dispatch(request, *args, **kwargs)


def _owned_conversation_or_404(user, pk) -> AIConversation:
    return get_object_or_404(AIConversation, pk=pk, owner=user)


class ChatPageView(AgentRequiredPageMixin, TemplateView):
    """Lands the user in their most recent conversation, or a fresh one."""

    template_name = "ai_agent/chat.html"

    def get(self, request, *args, **kwargs):
        requested_id = request.GET.get("c")
        if requested_id:
            conversation = _owned_conversation_or_404(request.user, requested_id)
        elif request.GET.get("new") == "1":
            conversation = AIConversation.objects.create(owner=request.user)
        else:
            conversation = (
                AIConversation.objects.filter(owner=request.user).order_by("-updated_at").first()
                or AIConversation.objects.create(owner=request.user)
            )
        self.conversation = conversation
        return super().get(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        from django.contrib import admin

        context = super().get_context_data(**kwargs)
        context.update(admin.site.each_context(self.request))
        context["title"] = "AI Assistant"
        context["conversation"] = self.conversation
        can_use_reports = self.request.user.has_perm("reports.use_reports")
        messages = []
        previous_question = ""
        for index, message in enumerate(
            (m for m in self.conversation.messages.all() if m.display_text), start=1
        ):
            item = {
                "index": index,
                "role": message.role,
                "text": message.display_text,
                "tool_calls": message.tool_calls,
                "chart": message.chart,
                "artifacts": message.artifacts,
            }
            if message.role == AIMessage.Role.USER:
                previous_question = message.display_text
            else:
                calls = message.tool_calls or []
                item["id"] = message.pk
                item["can_save"] = can_use_reports and any(
                    call.get("name") == "aggregate" for call in calls
                )
                item["save_name"] = previous_question[:200]
            messages.append(item)
        context["messages"] = messages
        context["charts_by_index"] = {
            message["index"]: message["chart"]
            for message in messages
            if message["chart"]
        }
        context["assistant_texts_by_index"] = {
            message["index"]: message["text"]
            for message in messages
            if message["role"] == "assistant"
        }
        context["new_conversation_url"] = reverse("admin:ai_agent_chat") + "?new=1"
        context["chat_base_url"] = reverse("admin:ai_agent_chat")
        context["send_url"] = reverse("ai_agent:send_message")
        context["conversation_list_url"] = reverse("ai_agent:conversation_list")
        context["save_report_url"] = reverse("ai_agent:save_report")
        claude_companies = companies_with_claude_key(self.request.user)
        saved = self.conversation.claude_company
        if saved is None or not any(company.pk == saved.pk for company in claude_companies):
            saved = claude_companies[0] if claude_companies else None
        context["claude_companies"] = claude_companies
        context["selected_claude_company_id"] = saved.pk if saved else None
        return context


def _sse(event: dict) -> str:
    return f"data: {json.dumps(event, default=str, ensure_ascii=False)}\n\n"


@method_decorator(csrf_protect, name="dispatch")
class SendMessageView(AgentRequiredJSONMixin, View):
    """POST {"conversation_id": int, "message": str} -> a live SSE stream
    of the agent's turn (steps, text deltas, then a final "done" event).
    """

    def post(self, request):
        try:
            body = json.loads(request.body)
        except json.JSONDecodeError:
            return JsonResponse({"error": str(_("Invalid JSON"))}, status=400)

        message_text = (body.get("message") or "").strip()
        conversation_id = body.get("conversation_id")
        if not message_text:
            return JsonResponse({"error": str(_("message is required"))}, status=400)
        if not conversation_id:
            return JsonResponse(
                {"error": str(_("conversation_id is required"))}, status=400
            )

        conversation = _owned_conversation_or_404(request.user, conversation_id)
        active_company_ids = [c.pk for c in request.active_companies] or None

        def events():
            ActiveCompany.set_current_ids(active_company_ids)
            try:
                try:
                    company = resolve_claude_company(
                        request.user,
                        body.get("company_id"),
                        preferred=conversation.claude_company,
                    )
                    service = ConversationService(request.user, company)
                except RuntimeError as exc:
                    logger.error("ConversationService error: %s", exc)
                    yield _sse({"type": "error", "message": str(exc)})
                    return
                if conversation.claude_company_id != company.pk:
                    conversation.claude_company = company
                    conversation.save(update_fields=["claude_company"])
                try:
                    for event in service.stream_turn(conversation, message_text):
                        yield _sse(event)
                except Exception:
                    logger.exception("Unexpected agent error")
                    yield _sse(
                        {
                            "type": "error",
                            "message": str(
                                _("An error occurred processing your request.")
                            ),
                        }
                    )
            finally:
                ActiveCompany.clear_current()

        response = StreamingHttpResponse(events(), content_type="text/event-stream")
        response["Cache-Control"] = "no-cache"
        response["X-Accel-Buffering"] = "no"
        return response


@method_decorator(csrf_protect, name="dispatch")
class SaveReportView(AgentRequiredJSONMixin, View):
    """POST {message_id, name, visibility} stores the reply's aggregate as a report."""

    def post(self, request):
        try:
            body = json.loads(request.body)
        except json.JSONDecodeError:
            return JsonResponse({"error": str(_("Invalid JSON"))}, status=400)

        message_id = body.get("message_id")
        if not message_id:
            return JsonResponse({"error": str(_("message_id is required"))}, status=400)

        message = get_object_or_404(
            AIMessage.objects.select_related("conversation"),
            pk=message_id,
            conversation__owner=request.user,
            role=AIMessage.Role.ASSISTANT,
        )
        prompt = (
            AIMessage.objects.filter(
                conversation=message.conversation,
                role=AIMessage.Role.USER,
                pk__lt=message.pk,
            )
            .exclude(display_text="")
            .order_by("-pk")
            .values_list("display_text", flat=True)
            .first()
            or ""
        )
        try:
            report = save_report_from_message(
                user=request.user,
                message=message,
                name=body.get("name") or "",
                visibility=body.get("visibility") or ReportVisibility.PRIVATE,
                prompt=prompt,
            )
        except SaveReportError as exc:
            return JsonResponse({"error": str(exc)}, status=400)
        except PermissionDenied:
            return JsonResponse(
                {"error": str(_("You cannot save reports."))}, status=403
            )

        return JsonResponse(
            {
                "id": report.pk,
                "name": report.name,
                "url": reverse("admin:reports_report_run", args=[report.pk]),
            }
        )


class ConversationListView(AgentRequiredJSONMixin, View):
    """GET -> the user's own conversations, newest first, for the history search."""

    def get(self, request):
        conversations = (
            AIConversation.objects.filter(owner=request.user)
            .order_by("-updated_at")
            .prefetch_related("messages")[:50]
        )
        data = []
        for conversation in conversations:
            first_user_text = next(
                (
                    m.display_text
                    for m in conversation.messages.all()
                    if m.role == AIMessage.Role.USER and m.display_text
                ),
                "",
            )
            title = conversation.title or first_user_text[:80] or str(conversation)
            data.append(
                {
                    "id": conversation.pk,
                    "title": title,
                    "updated_at": conversation.updated_at.strftime("%d/%m/%Y %H:%M"),
                }
            )
        return JsonResponse({"conversations": data})


class ArtifactDownloadView(AgentRequiredJSONMixin, View):
    def get(self, request, pk):
        artifact = get_object_or_404(
            AIArtifact, pk=pk, conversation__owner=request.user
        )
        if not artifact.file:
            raise Http404
        return FileResponse(
            artifact.file.open("rb"),
            as_attachment=True,
            filename=artifact.file.name.rsplit("/", 1)[-1],
        )
