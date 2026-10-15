from contextlib import contextmanager
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.test import SimpleTestCase, TestCase

from apps.ai_agent.models import AIConversation, AIMessage
from apps.ai_agent.orchestrator import (
    AGENT_EFFORT,
    MAX_HISTORY_MESSAGES,
    PROMPT_CACHE_CONTROL,
    THINKING_BETWEEN_TOOLS,
    ConversationService,
    chart_input_for_request,
    describe_tool_call,
    trim_history,
)
from company.models import Company, CompanyUser
from sales.models import Client


User = get_user_model()


def _text_block(text):
    return MagicMock(type="text", text=text)


def _tool_use_block(tool_id, name, input_):
    block = MagicMock(type="tool_use", id=tool_id, input=input_)
    block.name = name  # "name" is a reserved Mock() constructor kwarg
    return block


def _thinking_block(text, signature="sig-123"):
    return MagicMock(type="thinking", thinking=text, signature=signature)


def _content_block_start(index, block):
    return MagicMock(type="content_block_start", index=index, content_block=block)


def _content_block_stop(index, block):
    return MagicMock(type="content_block_stop", index=index, content_block=block)


def _text_event(text):
    return MagicMock(type="text", text=text)


def _fake_stream(events, final_content):
    """A stand-in for ``client.messages.stream(...)``'s context manager."""

    @contextmanager
    def _cm(**kwargs):
        stream = MagicMock()
        stream.__iter__.return_value = iter(events)
        stream.get_final_message.return_value = MagicMock(content=final_content)
        yield stream

    return _cm


class ConversationServiceStreamTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name="Orchestrator Co", tax_identifier="OR1")
        cls.user = User.objects.create_user(
            username="orchestrator-user", password="pass", is_staff=True
        )
        CompanyUser.objects.create(user=cls.user, company=cls.company, is_active=True)
        perm = Permission.objects.get(
            content_type__app_label="sales", codename="view_client"
        )
        cls.user.user_permissions.add(perm)
        cls.client_obj = Client.objects.create(company=cls.company, name="Orchestrator Client")

    def setUp(self):
        self.company.set_claude_api_key("sk-ant-test")
        self.company.save(update_fields=["claude_api_key_encrypted"])
        self.conversation = AIConversation.objects.create(owner=self.user)

    def _mock_client(self, mock_anthropic_cls, stream_fn):
        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client
        mock_client.messages.stream.side_effect = stream_fn
        return mock_client

    @patch("apps.ai_agent.orchestrator.anthropic.Anthropic")
    def test_direct_text_reply_streams_deltas_and_persists_two_messages(
        self, mock_anthropic_cls
    ):
        events = [_text_event("Hola, "), _text_event("¿en qué te ayudo?")]
        final_content = [_text_block("Hola, ¿en qué te ayudo?")]
        self._mock_client(mock_anthropic_cls, _fake_stream(events, final_content))

        events_seen = list(
            ConversationService(self.user).stream_turn(self.conversation, "Hola")
        )

        text_deltas = [e["text"] for e in events_seen if e["type"] == "text_delta"]
        self.assertEqual(text_deltas, ["Hola, ", "¿en qué te ayudo?"])
        done = events_seen[-1]
        self.assertEqual(done["type"], "done")
        self.assertEqual(done["response"], "Hola, ¿en qué te ayudo?")
        self.assertEqual(self.conversation.messages.count(), 2)
        self.assertEqual(
            list(self.conversation.messages.values_list("role", flat=True)),
            [AIMessage.Role.USER, AIMessage.Role.ASSISTANT],
        )
        mock_anthropic_cls.assert_called_once_with(api_key="sk-ant-test")
        kwargs = mock_anthropic_cls.return_value.messages.stream.call_args.kwargs
        self.assertEqual(kwargs["output_config"], {"effort": AGENT_EFFORT})
        self.assertEqual(kwargs["thinking"], THINKING_BETWEEN_TOOLS)
        self.assertEqual(kwargs["cache_control"], PROMPT_CACHE_CONTROL)
        self.assertEqual(kwargs["system"][0]["cache_control"], PROMPT_CACHE_CONTROL)
        self.assertNotIn("cache_control", kwargs["system"][1])
        self.assertIn("Orchestrator Co", kwargs["system"][1]["text"])
        self.assertEqual(kwargs["tools"][-1]["cache_control"], PROMPT_CACHE_CONTROL)

    @patch("apps.ai_agent.orchestrator.anthropic.Anthropic")
    def test_system_prompt_names_only_the_active_company_not_every_allowed_one(
        self, mock_anthropic_cls
    ):
        other_company = Company.objects.create(name="Other Co", tax_identifier="OT1")
        CompanyUser.objects.create(
            user=self.user, company=other_company, is_active=True
        )
        from company.active_company import ActiveCompany

        ActiveCompany.set_current_ids([self.company.pk])
        self.addCleanup(ActiveCompany.clear_current)

        events = [_text_event("Hola")]
        final_content = [_text_block("Hola")]
        self._mock_client(mock_anthropic_cls, _fake_stream(events, final_content))

        list(ConversationService(self.user).stream_turn(self.conversation, "Hola"))

        kwargs = mock_anthropic_cls.return_value.messages.stream.call_args.kwargs
        scope_text = kwargs["system"][1]["text"]
        self.assertIn("Orchestrator Co", scope_text)
        self.assertNotIn("Other Co", scope_text)

    @patch("apps.ai_agent.orchestrator.anthropic.Anthropic")
    def test_long_history_is_trimmed_before_the_model_call(self, mock_anthropic_cls):
        for index in range(12):
            AIMessage.objects.create(
                conversation=self.conversation,
                role=AIMessage.Role.USER,
                content=[{"type": "text", "text": f"q{index}"}],
                display_text=f"q{index}",
            )
            AIMessage.objects.create(
                conversation=self.conversation,
                role=AIMessage.Role.ASSISTANT,
                content=[{"type": "text", "text": f"a{index}"}],
                display_text=f"a{index}",
            )
        self._mock_client(
            mock_anthropic_cls,
            _fake_stream([_text_event("ok")], [_text_block("ok")]),
        )

        list(ConversationService(self.user).stream_turn(self.conversation, "última"))

        sent = mock_anthropic_cls.return_value.messages.stream.call_args.kwargs[
            "messages"
        ]
        # Only the trimmed window plus the new question goes to the model;
        # which turns survive is HistoryTrimTests' business.
        self.assertEqual(len(sent), MAX_HISTORY_MESSAGES + 1)
        self.assertEqual(sent[-1]["content"][0]["text"], "última")
        self.assertEqual(self.conversation.messages.count(), 26)

    @patch("apps.ai_agent.orchestrator.anthropic.Anthropic")
    def test_tool_round_then_final_text(self, mock_anthropic_cls):
        tool_block = _tool_use_block("t1", "list_datasets", {})
        round1 = [_content_block_start(0, tool_block), _content_block_stop(0, tool_block)]
        round2 = [_text_event("Aquí tienes los datasets.")]
        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client
        mock_client.messages.stream.side_effect = [
            _fake_stream(round1, [tool_block])(),
            _fake_stream(round2, [_text_block("Aquí tienes los datasets.")])(),
        ]

        events_seen = list(
            ConversationService(self.user).stream_turn(self.conversation, "¿Qué puedo consultar?")
        )

        step_starts = [e for e in events_seen if e["type"] == "step_start"]
        step_dones = [e for e in events_seen if e["type"] == "step_done"]
        self.assertEqual(len(step_starts), 1)
        self.assertEqual(len(step_dones), 1)
        self.assertTrue(step_dones[0]["success"])

        done = events_seen[-1]
        self.assertEqual(done["response"], "Aquí tienes los datasets.")
        self.assertEqual(done["tool_calls"], [{"name": "list_datasets", "input": {}}])

        roles = list(self.conversation.messages.values_list("role", flat=True))
        self.assertEqual(
            roles,
            [
                AIMessage.Role.USER,
                AIMessage.Role.ASSISTANT,
                AIMessage.Role.USER,
                AIMessage.Role.ASSISTANT,
            ],
        )

    @patch("apps.ai_agent.orchestrator.anthropic.Anthropic")
    def test_thinking_block_emits_step_and_is_replayed_verbatim(
        self, mock_anthropic_cls
    ):
        think_block = _thinking_block("hmm, let me check", signature="sig-abc")
        tool_block = _tool_use_block("t1", "list_datasets", {})
        round1 = [
            _content_block_start(0, think_block),
            _content_block_start(1, tool_block),
            _content_block_stop(1, tool_block),
        ]
        round2 = [_text_event("Listo.")]
        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client
        mock_client.messages.stream.side_effect = [
            _fake_stream(round1, [think_block, tool_block])(),
            _fake_stream(round2, [_text_block("Listo.")])(),
        ]

        events_seen = list(
            ConversationService(self.user).stream_turn(self.conversation, "hola")
        )

        thinking_steps = [
            e for e in events_seen if e["type"] == "step_start" and e["label"] == "Pensando"
        ]
        self.assertEqual(len(thinking_steps), 1)

        # The assistant message carrying the thinking block must replay it
        # verbatim (thinking text + signature) in the next call's history.
        second_call_messages = mock_client.messages.stream.call_args_list[1].kwargs["messages"]
        assistant_turn = second_call_messages[1]
        thinking_payload = assistant_turn["content"][0]
        self.assertEqual(thinking_payload["type"], "thinking")
        self.assertEqual(thinking_payload["thinking"], "hmm, let me check")
        self.assertEqual(thinking_payload["signature"], "sig-abc")

    @patch("apps.ai_agent.orchestrator.anthropic.Anthropic")
    def test_get_entity_tool_call_sets_active_state(self, mock_anthropic_cls):
        tool_block = _tool_use_block(
            "t1", "get_entity", {"dataset": "clients", "id": self.client_obj.pk}
        )
        round1 = [_content_block_start(0, tool_block), _content_block_stop(0, tool_block)]
        round2 = [_text_event("Ese es el cliente.")]
        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client
        mock_client.messages.stream.side_effect = [
            _fake_stream(round1, [tool_block])(),
            _fake_stream(round2, [_text_block("Ese es el cliente.")])(),
        ]

        list(ConversationService(self.user).stream_turn(self.conversation, "Háblame de ese cliente"))

        self.conversation.refresh_from_db()
        active = self.conversation.state["active"]["clients"]
        self.assertEqual(active["id"], self.client_obj.pk)
        self.assertEqual(active["label"], "Orchestrator Client")

    @patch("apps.ai_agent.orchestrator.anthropic.Anthropic")
    def test_max_tool_rounds_safety_valve(self, mock_anthropic_cls):
        def infinite_tool_round(**kwargs):
            block = _tool_use_block("t", "list_datasets", {})
            return _fake_stream(
                [_content_block_start(0, block), _content_block_stop(0, block)],
                [block],
            )()

        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client
        mock_client.messages.stream.side_effect = infinite_tool_round

        events_seen = list(
            ConversationService(self.user).stream_turn(self.conversation, "bucle infinito")
        )
        done = events_seen[-1]
        self.assertEqual(done["type"], "done")
        self.assertIn("maximum number of tool calls", done["response"])

    @patch("apps.ai_agent.orchestrator.anthropic.Anthropic")
    def test_error_during_stream_yields_error_event(self, mock_anthropic_cls):
        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client
        mock_client.messages.stream.side_effect = RuntimeError("boom")

        events_seen = list(ConversationService(self.user).stream_turn(self.conversation, "hola"))
        self.assertEqual(events_seen[-1], {"type": "error", "message": "boom"})

    def test_missing_api_key_raises_runtime_error(self):
        self.company.set_claude_api_key("")
        self.company.save(update_fields=["claude_api_key_encrypted"])
        with self.assertRaises(RuntimeError):
            ConversationService(self.user)


class HistoryTrimTests(SimpleTestCase):
    def test_short_history_is_unchanged(self):
        messages = [_user("hola"), _assistant("qué tal")]
        self.assertEqual(trim_history(messages, limit=16), messages)

    def test_drops_older_turns_on_a_user_question(self):
        messages = []
        for index in range(6):
            messages.append(_user(f"q{index}"))
            messages.append(_assistant(f"a{index}"))
        trimmed = trim_history(messages, limit=4)
        self.assertEqual(trimmed[0]["content"][0]["text"], "q4")
        self.assertEqual(len(trimmed), 4)

    def test_does_not_split_a_tool_result_from_its_question(self):
        messages = [
            _user("vieja"),
            _assistant("respuesta vieja"),
            _user("nueva"),
            _assistant("voy a consultar"),
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "t", "content": "{}"}
                ],
            },
            _assistant("hecho"),
        ]
        trimmed = trim_history(messages, limit=3)
        self.assertEqual(trimmed[0]["content"][0]["text"], "nueva")
        self.assertEqual(len(trimmed), 4)


def _user(text):
    return {"role": "user", "content": [{"type": "text", "text": text}]}


def _assistant(text):
    return {"role": "assistant", "content": [{"type": "text", "text": text}]}


class DescribeToolCallTests(TestCase):
    def test_dataset_aware_labels_include_the_dataset_name(self):
        label = describe_tool_call("aggregate", {"dataset": "sales_invoices"})
        self.assertTrue(label.startswith("Calculando totales en"))
        self.assertNotEqual(label, "Calculando totales en")

    def test_falls_back_without_dataset(self):
        self.assertEqual(describe_tool_call("list_datasets", {}), "Revisando qué puedo consultar")

    def test_unknown_tool_has_generic_label(self):
        self.assertEqual(describe_tool_call("mystery_tool", {}), "Usando mystery_tool")


class TimeChartTests(SimpleTestCase):
    def test_a_monthly_bar_becomes_a_line_unless_bars_were_requested(self):
        tool_input = {
            "chart": "bar",
            "dimensions": ["date_issued_month"],
            "chart_label_dimension": "date_issued_month",
            "chart_value_aggregation": "facturado",
        }
        question = (
            "puedes hacer un grafico de la facturacion de san carlos "
            "este ultimo año agrupado por mes?"
        )
        updated = chart_input_for_request(question, tool_input)
        self.assertEqual(updated["chart"], "line")

        bars = chart_input_for_request("puedes hacer un grafico de barras?", tool_input)
        self.assertEqual(bars["chart"], "bar")

    def test_a_client_comparison_stays_a_bar(self):
        tool_input = {
            "chart": "bar",
            "dimensions": ["client"],
            "chart_label_dimension": "client",
        }
        updated = chart_input_for_request("quien ha facturado mas", tool_input)
        self.assertEqual(updated["chart"], "bar")
