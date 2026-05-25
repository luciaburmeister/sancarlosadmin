"""
Chatbot views — admin-authenticated endpoints.

All endpoints require Django session authentication (the user must be
logged into the admin).
"""

from __future__ import annotations

import json
import logging
import uuid

from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import JsonResponse
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.csrf import csrf_protect

from chatbot.interfaces import ChatContext
from chatbot.services.chat_service import ChatService
from company.models import Company, CompanyUser

logger = logging.getLogger(__name__)


class StaffRequiredMixin(LoginRequiredMixin):
    """Restrict access to staff users with the ``chatbot.use_chatbot`` permission."""

    def dispatch(self, request, *args, **kwargs):
        if not request.user.is_authenticated or not request.user.is_staff:
            return JsonResponse({"error": "Authentication required."}, status=403)
        if not request.user.has_perm("chatbot.use_chatbot"):
            return JsonResponse({"error": "Chatbot access denied."}, status=403)
        return super().dispatch(request, *args, **kwargs)


class CompanyAccessMixin:
    """Shared helper for verifying company access."""

    def get_user_companies(self, user):
        """Return a queryset of active companies the user can access."""
        if user.is_superuser:
            return Company.objects.filter(is_active=True).order_by("name")

        company_ids = CompanyUser.objects.filter(
            user=user, is_active=True
        ).values_list("company_id", flat=True)
        return Company.objects.filter(
            pk__in=company_ids, is_active=True
        ).order_by("name")

    def get_authorized_company(self, user, company_id):
        """Return the Company if the user has access, or None."""
        try:
            company = Company.objects.get(pk=company_id, is_active=True)
        except Company.DoesNotExist:
            return None

        if user.is_superuser:
            return company

        has_access = CompanyUser.objects.filter(
            user=user, company_id=company_id, is_active=True
        ).exists()
        return company if has_access else None


class CompaniesView(StaffRequiredMixin, CompanyAccessMixin, View):
    """Return companies the logged-in user has access to."""

    def get(self, request):
        companies = self.get_user_companies(request.user)
        data = [{"id": c.id, "name": c.name} for c in companies]
        return JsonResponse({"companies": data})


@method_decorator(csrf_protect, name="dispatch")
class ChatView(StaffRequiredMixin, CompanyAccessMixin, View):
    """Process a chat message and return the LLM response.

    Expects JSON body::

        {
            "company_id": 1,
            "message": "Show me all clients",
            "conversation_id": "optional-uuid",
            "history": [...]
        }
    """

    def post(self, request):
        # ── Parse body ────────────────────────────────────────────
        try:
            body = json.loads(request.body)
        except json.JSONDecodeError:
            return JsonResponse({"error": "Invalid JSON"}, status=400)

        company_id = body.get("company_id")
        message = body.get("message", "").strip()
        conversation_id = body.get("conversation_id") or str(uuid.uuid4())
        history = body.get("history") or []

        if not company_id:
            return JsonResponse({"error": "company_id is required"}, status=400)
        if not message:
            return JsonResponse({"error": "message is required"}, status=400)

        # ── Verify company access ─────────────────────────────────
        company = self.get_authorized_company(request.user, company_id)
        if company is None:
            return JsonResponse(
                {"error": "Company not found or access denied."}, status=403
            )

        # ── Build context & call LLM ──────────────────────────────
        context = ChatContext(
            company_id=company.id,
            user_id=request.user.id,
            company_name=company.name,
        )

        try:
            service = ChatService()
            result = service.chat(context=context, message=message, history=history)
        except RuntimeError as e:
            logger.error("ChatService error: %s", e)
            return JsonResponse({"error": str(e)}, status=500)
        except Exception:
            logger.exception("Unexpected chat error")
            return JsonResponse(
                {"error": "An error occurred processing your request."}, status=500
            )

        return JsonResponse({
            "response": result["response"],
            "tools_used": result["tools_used"],
            "conversation_id": conversation_id,
            "history": self._serialize_history(result["history"]),
        })

    # ── Private helpers ───────────────────────────────────────────

    @staticmethod
    def _serialize_history(history: list) -> list:
        """Convert Claude content blocks to JSON-serialisable form."""
        serialized = []
        for msg in history:
            role = msg.get("role", "")
            content = msg.get("content", "")

            if isinstance(content, list):
                serialized_content = []
                for block in content:
                    if hasattr(block, "type"):
                        # Claude API object → dict
                        if block.type == "text":
                            serialized_content.append({
                                "type": "text",
                                "text": block.text,
                            })
                        elif block.type == "tool_use":
                            serialized_content.append({
                                "type": "tool_use",
                                "id": block.id,
                                "name": block.name,
                                "input": block.input,
                            })
                        elif block.type == "tool_result":
                            serialized_content.append(block)
                        else:
                            serialized_content.append({
                                "type": block.type,
                                "text": getattr(block, "text", ""),
                            })
                    elif isinstance(block, dict):
                        serialized_content.append(block)
                    else:
                        serialized_content.append({
                            "type": "text",
                            "text": str(block),
                        })
                serialized.append({"role": role, "content": serialized_content})
            else:
                serialized.append({"role": role, "content": content})

        return serialized
