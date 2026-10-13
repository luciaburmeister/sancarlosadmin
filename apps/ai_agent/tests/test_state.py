from django.test import SimpleTestCase

from apps.ai_agent.state import ConversationState


class ConversationStateTests(SimpleTestCase):
    def test_round_trips_through_dict(self):
        state = ConversationState()
        state.set_active("clients", 42, "Acme SL")
        state.set_focused("sales_invoices", [1, 2, 3], "pendientes de cobro")

        restored = ConversationState.from_dict(state.to_dict())

        self.assertEqual(restored.active["clients"].id, 42)
        self.assertEqual(restored.active["clients"].label, "Acme SL")
        self.assertEqual(restored.focused.dataset, "sales_invoices")
        self.assertEqual(restored.focused.ids, [1, 2, 3])

    def test_empty_state_summary_says_nothing_is_focused(self):
        self.assertIn("Nothing", ConversationState().to_prompt_summary())

    def test_summary_mentions_active_and_focused(self):
        state = ConversationState()
        state.set_active("clients", 42, "Acme SL")
        state.set_focused("sales_invoices", [1, 2])
        summary = state.to_prompt_summary()
        self.assertIn("clients#42", summary)
        self.assertIn("sales_invoices ids=[1, 2]", summary)

    def test_focused_ids_are_capped(self):
        state = ConversationState()
        state.set_focused("sales_invoices", list(range(100)))
        self.assertEqual(len(state.focused.ids), 50)

    def test_from_dict_handles_empty_input(self):
        state = ConversationState.from_dict(None)
        self.assertEqual(state.active, {})
        self.assertIsNone(state.focused)
