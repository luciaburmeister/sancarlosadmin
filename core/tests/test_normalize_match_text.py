from django.test import SimpleTestCase

from core.text import join_unique, normalize_match_data, normalize_match_text


class JoinUniqueTests(SimpleTestCase):
    def test_strips_blanks_and_duplicates(self):
        self.assertEqual(
            join_unique([" Málaga ", "Málaga", "", None, " Antequera "]),
            "Málaga, Antequera",
        )
        self.assertEqual(join_unique([]), "")


class NormalizeMatchTextTests(SimpleTestCase):
    def test_decodes_accent_and_ordinal_escapes(self):
        self.assertEqual(normalize_match_text("Hormig\\u00f3n"), "Hormigón")
        self.assertEqual(normalize_match_text("2\\u00ba RCD"), "2º RCD")

    def test_leaves_plain_text_and_numbers(self):
        self.assertEqual(normalize_match_text("Hormigón"), "Hormigón")
        self.assertEqual(normalize_match_text("9800"), "9800")
        self.assertEqual(normalize_match_text(""), "")
        self.assertIsNone(normalize_match_text(None))

    def test_walks_nested_json(self):
        self.assertEqual(
            normalize_match_data({"material": "cart\\u00f3n", "lines": ["2\\u00ba RCD"], "qty": 1}),
            {"material": "cartón", "lines": ["2º RCD"], "qty": 1},
        )
