import base64

from django.template import engines
from django.test import SimpleTestCase

from configurator.templatetags.formatting import qr


class QrTemplateFilterTests(SimpleTestCase):
    def test_encodes_a_link_as_a_png_data_uri(self):
        uri = qr("https://erp.example.com/es/doc/12")

        self.assertTrue(uri.startswith("data:image/png;base64,"))
        png = base64.b64decode(uri.removeprefix("data:image/png;base64,"))
        self.assertTrue(png.startswith(b"\x89PNG"))

    def test_empty_value_renders_nothing(self):
        self.assertEqual(qr(""), "")
        self.assertEqual(qr(None), "")
        self.assertEqual(qr("   "), "")

    def test_export_templates_can_use_it_without_load(self):
        template = engines["django"].from_string('<img src="{{ url|qr }}">')

        html = template.render({"url": "https://erp.example.com/es/doc/12"})
        self.assertIn('src="data:image/png;base64,', html)

        empty = template.render({"url": ""})
        self.assertIn('src=""', empty)
