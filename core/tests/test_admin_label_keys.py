"""
Unfold colours a badge by looking the displayed value up in the ``label``
dict. A lazily translated key is hashed once, in whatever language was active
when the admin module was imported, so a badge rendered in another language
(or after the catalogue changes) never matches and turns grey.

Badges key their colours on the stored value instead and return
``(value, translated text)``.

    python manage.py test core.tests.test_admin_label_keys
"""
from django.contrib.admin import site
from django.test import SimpleTestCase
from django.utils.functional import Promise


class AdminLabelKeysTest(SimpleTestCase):

    def test_no_badge_colour_is_keyed_by_translated_text(self):
        offenders = []
        for model_admin in site._registry.values():
            cls = type(model_admin)
            for name in dir(cls):
                label = getattr(getattr(cls, name, None), "label", None)
                if isinstance(label, dict):
                    offenders += [
                        f"{cls.__module__}.{cls.__name__}.{name}: {key!r}"
                        for key in label if isinstance(key, Promise)
                    ]
        self.assertEqual(offenders, [], "Key badge colours by value and return (value, text).")
