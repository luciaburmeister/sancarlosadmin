import json
import time
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.cache import cache
from django.core.files.base import ContentFile
from django.test import TestCase
from django.urls import reverse

from apps.ai_agent.models import AIArtifact, AIConversation, AIMessage
from company.active_company import ActiveCompany
from company.models import Company, CompanyUser

User = get_user_model()


class ChatViewsPermissionTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(
            username="chat-user", password="pass", is_staff=True
        )
        cls.permitted_user = User.objects.create_user(
            username="permitted-user", password="pass", is_staff=True
        )
        perm = Permission.objects.get(
            content_type__app_label="ai_agent", codename="use_agent"
        )
        cls.permitted_user.user_permissions.add(perm)

    def test_chat_page_denies_without_permission(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse("admin:ai_agent_chat"))
        self.assertEqual(response.status_code, 403)

    def test_chat_page_allows_with_permission(self):
        self.client.force_login(self.permitted_user)
        response = self.client.get(reverse("admin:ai_agent_chat"))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(
            AIConversation.objects.filter(owner=self.permitted_user).exists()
        )

    def test_chat_page_new_creates_a_fresh_conversation(self):
        self.client.force_login(self.permitted_user)
        self.client.get(reverse("admin:ai_agent_chat"))
        self.assertEqual(
            AIConversation.objects.filter(owner=self.permitted_user).count(), 1
        )
        self.client.get(reverse("admin:ai_agent_chat") + "?new=1")
        self.assertEqual(
            AIConversation.objects.filter(owner=self.permitted_user).count(), 2
        )

    def test_chat_page_loads_a_specific_owned_conversation(self):
        self.client.force_login(self.permitted_user)
        conversation = AIConversation.objects.create(owner=self.permitted_user)
        response = self.client.get(reverse("admin:ai_agent_chat") + f"?c={conversation.pk}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["conversation"].pk, conversation.pk)

    def test_chat_page_denies_another_users_conversation_via_c_param(self):
        other = User.objects.create_user(username="c-param-other", password="pass", is_staff=True)
        conversation = AIConversation.objects.create(owner=other)
        self.client.force_login(self.permitted_user)
        response = self.client.get(reverse("admin:ai_agent_chat") + f"?c={conversation.pk}")
        self.assertEqual(response.status_code, 404)

    def test_conversation_list_only_returns_the_caller_own_conversations(self):
        other = User.objects.create_user(username="list-other", password="pass", is_staff=True)
        AIConversation.objects.create(owner=other, title="Not mine")
        mine = AIConversation.objects.create(owner=self.permitted_user, title="Mine")

        self.client.force_login(self.permitted_user)
        response = self.client.get(reverse("ai_agent:conversation_list"))

        self.assertEqual(response.status_code, 200)
        ids = [c["id"] for c in response.json()["conversations"]]
        self.assertEqual(ids, [mine.pk])

    def test_send_message_denies_without_permission(self):
        self.client.force_login(self.user)
        response = self.client.post(
            reverse("ai_agent:send_message"),
            data=json.dumps({"conversation_id": 1, "message": "hola"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 403)

    def _company_with_key(self, name, tax_id, key="sk-ant-test"):
        company = Company.objects.create(name=name, tax_identifier=tax_id)
        company.set_claude_api_key(key)
        company.save(update_fields=["claude_api_key_encrypted"])
        CompanyUser.objects.create(
            user=self.permitted_user, company=company, is_active=True
        )
        cache.clear()
        return company

    def _activate(self, *companies):
        session = self.client.session
        session[ActiveCompany.SESSION_KEY] = [company.pk for company in companies]
        session[ActiveCompany.SESSION_SET_AT_KEY] = time.time()
        session.save()

    @patch("apps.ai_agent.views.ConversationService")
    def test_send_message_streams_sse_events_for_own_conversation(
        self, mock_service_cls
    ):
        self._company_with_key("Chat Co", "CC1")
        conversation = AIConversation.objects.create(owner=self.permitted_user)
        mock_service = mock_service_cls.return_value
        mock_service.stream_turn.return_value = iter(
            [
                {"type": "step_start", "label": "Revisando qué puedo consultar"},
                {"type": "step_done", "label": "Revisando qué puedo consultar", "success": True},
                {"type": "text_delta", "text": "Hola"},
                {"type": "text_delta", "text": "!"},
                {
                    "type": "done",
                    "response": "Hola!",
                    "tool_calls": [{"name": "list_datasets", "input": {}}],
                    "chart": None,
                    "artifacts": [],
                },
            ]
        )

        self.client.force_login(self.permitted_user)
        response = self.client.post(
            reverse("ai_agent:send_message"),
            data=json.dumps(
                {"conversation_id": conversation.pk, "message": "hola"}
            ),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "text/event-stream")
        body = b"".join(response.streaming_content).decode()
        events = [
            json.loads(chunk[len("data: "):])
            for chunk in body.split("\n\n")
            if chunk.startswith("data: ")
        ]
        self.assertEqual(events[0]["type"], "step_start")
        self.assertEqual(events[-1]["type"], "done")
        self.assertEqual(events[-1]["response"], "Hola!")
        mock_service.stream_turn.assert_called_once_with(conversation, "hola")

    @patch("apps.ai_agent.views.ConversationService")
    def test_send_message_uses_the_chosen_company_key(self, mock_service_cls):
        first = self._company_with_key("Alpha Co", "CC2", key="sk-alpha")
        second = self._company_with_key("Beta Co", "CC3", key="sk-beta")
        conversation = AIConversation.objects.create(owner=self.permitted_user)
        mock_service_cls.return_value.stream_turn.return_value = iter(
            [{"type": "done", "response": "ok", "tool_calls": [], "chart": None, "artifacts": []}]
        )

        self.client.force_login(self.permitted_user)
        self._activate(first, second)
        response = self.client.post(
            reverse("ai_agent:send_message"),
            data=json.dumps(
                {
                    "conversation_id": conversation.pk,
                    "message": "hola",
                    "company_id": second.pk,
                }
            ),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        b"".join(response.streaming_content)
        company = mock_service_cls.call_args.args[1]
        self.assertEqual(company.pk, second.pk)
        conversation.refresh_from_db()
        self.assertEqual(conversation.claude_company_id, second.pk)
        self.assertNotEqual(conversation.claude_company_id, first.pk)

    @patch("apps.ai_agent.orchestrator.anthropic.Anthropic")
    def test_send_message_scopes_the_live_stream_to_the_active_companies_only(
        self, mock_anthropic_cls
    ):
        """The sidebar checkbox (session-active companies), not every
        company the user is allowed to see, must reach the real streaming
        body — which runs after ActiveCompanyMiddleware's own cleanup."""
        from contextlib import contextmanager

        active = self._company_with_key("Active Co", "AC1")
        other = self._company_with_key("Other Co", "OC1")
        self._activate(active)

        mock_client = mock_anthropic_cls.return_value

        @contextmanager
        def fake_stream(**kwargs):
            stream = MagicMock()
            stream.__iter__.return_value = iter([])
            stream.get_final_message.return_value = MagicMock(
                content=[MagicMock(type="text", text="ok")]
            )
            self.captured_system = kwargs["system"]
            yield stream

        mock_client.messages.stream.side_effect = fake_stream

        conversation = AIConversation.objects.create(owner=self.permitted_user)
        self.client.force_login(self.permitted_user)
        response = self.client.post(
            reverse("ai_agent:send_message"),
            data=json.dumps(
                {"conversation_id": conversation.pk, "message": "hola"}
            ),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        b"".join(response.streaming_content)

        scope_text = self.captured_system[1]["text"]
        self.assertIn("Active Co", scope_text)
        self.assertNotIn("Other Co", scope_text)

    def test_chat_page_defaults_to_the_first_active_company_with_a_key(self):
        no_key = Company.objects.create(name="Alpha Co", tax_identifier="CC4")
        CompanyUser.objects.create(
            user=self.permitted_user, company=no_key, is_active=True
        )
        cache.clear()
        with_key = self._company_with_key("Zulu Co", "CC5", key="sk-zulu")
        self.client.force_login(self.permitted_user)
        self._activate(no_key, with_key)
        response = self.client.get(reverse("admin:ai_agent_chat"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [company.name for company in response.context["claude_companies"]],
            ["Zulu Co"],
        )
        self.assertEqual(response.context["selected_claude_company_id"], with_key.pk)
        self.assertContains(response, "Zulu Co")

    def test_chat_page_claude_company_dropdown_is_independent_of_active_selection(self):
        """The Claude-key selector lists every company the user has access
        to that has a key, regardless of which companies are toggled on in
        the session's active-company switcher."""
        active = self._company_with_key("Active Co", "CC6", key="sk-active")
        inactive = self._company_with_key("Inactive Co", "CC7", key="sk-inactive")
        self.client.force_login(self.permitted_user)
        self._activate(active)

        response = self.client.get(reverse("admin:ai_agent_chat"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            sorted(company.name for company in response.context["claude_companies"]),
            ["Active Co", "Inactive Co"],
        )
        self.assertContains(response, "Inactive Co")

    def test_send_message_denies_another_users_conversation(self):
        other = User.objects.create_user(username="other", password="pass", is_staff=True)
        conversation = AIConversation.objects.create(owner=other)

        self.client.force_login(self.permitted_user)
        response = self.client.post(
            reverse("ai_agent:send_message"),
            data=json.dumps(
                {"conversation_id": conversation.pk, "message": "hola"}
            ),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 404)


class ArtifactDownloadViewTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_user(
            username="artifact-owner", password="pass", is_staff=True
        )
        perm = Permission.objects.get(
            content_type__app_label="ai_agent", codename="use_agent"
        )
        cls.owner.user_permissions.add(perm)
        cls.other = User.objects.create_user(
            username="artifact-other", password="pass", is_staff=True
        )
        cls.other.user_permissions.add(perm)

        conversation = AIConversation.objects.create(owner=cls.owner)
        cls.artifact = AIArtifact.objects.create(
            conversation=conversation, kind="pdf", title="Report"
        )
        cls.artifact.file.save("report.pdf", ContentFile(b"%PDF-1.4 fake"), save=True)

    def test_owner_can_download(self):
        self.client.force_login(self.owner)
        response = self.client.get(
            reverse("ai_agent:download_artifact", args=[self.artifact.pk])
        )
        self.assertEqual(response.status_code, 200)

    def test_other_user_gets_404(self):
        self.client.force_login(self.other)
        response = self.client.get(
            reverse("ai_agent:download_artifact", args=[self.artifact.pk])
        )
        self.assertEqual(response.status_code, 404)
