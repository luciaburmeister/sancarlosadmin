"""
company/tests_unfold_overrides.py

Guards for the project's overrides of django-unfold templates under
templates/unfold/helpers/. Unfold ships a precompiled stylesheet and its
Alpine state lives in its own app.js, so when a vendor template renames a
variable or drops a feature an override keeps rendering with no server
error: the UI just silently stops working. That is what happened on the
0.99 -> 0.106 upgrade (the header started toggling `siteDropdownOpen`
while the override still showed on `openDropdown`, so the company
switcher never opened; and multi-column fieldset rows were removed).

Two layers of protection:

* Behavior: render real admin pages and assert what each override exists
  to provide (side-by-side fieldset rows; a switcher that opens on the
  same flag the header toggles and posts the whole selection at once).
* Drift: pinned hashes of the vendor files each override was synced
  against. When an Unfold upgrade changes one of them the test fails and
  names the pair of files to diff. Re-sync the override, then re-pin.

Run just this file:
    python manage.py test company.tests_unfold_overrides
"""
import hashlib
import importlib.metadata
import pathlib
import re

import unfold
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.staticfiles import finders
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse

from api.fleet.tests.helpers import make_company

User = get_user_model()

VENDOR_TEMPLATES = pathlib.Path(unfold.__file__).parent / "templates"
PROJECT_TEMPLATES = pathlib.Path(settings.BASE_DIR) / "templates"

# sha256 of the vendor files as shipped by the Unfold release the overrides
# were last synced against. navigation_header.html is not overridden, but
# it is the template that toggles the flag site_dropdown.html shows on, so
# a change there is just as likely to break the switcher.
PINNED_UNFOLD_VERSION = "0.106.0"
PINNED_VENDOR_HASHES = {
    "unfold/helpers/fieldset_row.html": "2eb8a8acf2d7538566aacb635f5a017374f8f47f026e10ab471c10ed87d44a42",
    "unfold/helpers/site_dropdown.html": "d2df866c72627d4a1b16bc5ac00684247bd3b35dbc3e1da515548d36b5c1d619",
    "unfold/helpers/site_logo.html": "4bf409a13daa874c70d1c89a2b1a13e0627a4081f97aa811482d1a6db59690e2",
    "unfold/helpers/navigation_header.html": "b2b4cec8f441ffa8c52ae57a8b336f146d87291abda89e8945364355da0c1111",
}

FORM_ROW_CLASSES = re.compile(r'<div class="([^"]*\bform-row\b[^"]*)"')


def read_static(path):
    location = finders.find(path)
    assert location, f"static file {path!r} not found by the staticfiles finders"
    return pathlib.Path(location).read_text(encoding="utf-8")


class UnfoldOverrideTestCase(TestCase):
    def setUp(self):
        cache.clear()  # LocMem cache persists across tests in-process
        self.company_a = make_company(name="Alpha")
        self.company_b = make_company(name="Beta")
        self.superuser = User.objects.create_superuser("super", "s@example.com", "x")
        self.client.force_login(self.superuser)


class FieldsetRowOverrideTests(UnfoldOverrideTestCase):
    """templates/unfold/helpers/fieldset_row.html restores multi-column rows."""

    def test_tuple_rows_render_side_by_side(self):
        # CompanyAdmin declares ("name", "legal_name") and other pairs, plus
        # single fields such as "address" in the same fieldset.
        response = self.client.get(reverse("admin:company_company_add"))
        self.assertEqual(response.status_code, 200)

        rows = [set(m.split()) for m in FORM_ROW_CLASSES.findall(response.content.decode())]
        self.assertTrue(rows, "no fieldset rows rendered on the Company add form")

        two_column = [r for r in rows if {"grid", "lg:grid-cols-2"} <= r]
        single = [r for r in rows if not any(c.startswith("lg:grid-cols-") for c in r)]
        self.assertTrue(
            two_column,
            "no row carries 'grid lg:grid-cols-2'; the fieldset_row.html "
            "override is not being used or lost its grid classes",
        )
        self.assertTrue(single, "single-field rows must not receive grid classes")
        for row in two_column:
            self.assertNotIn("lg:grid-cols-1", row)

    def test_grid_utilities_exist_in_unfold_stylesheet(self):
        # The override relies on utilities from Unfold's precompiled CSS. If a
        # release purges them the markup would be right but the layout gone.
        css = read_static("unfold/css/styles.css")
        for columns in (2, 3, 4):
            self.assertIn(
                f"lg\\:grid-cols-{columns}",
                css,
                f"unfold/css/styles.css no longer ships lg:grid-cols-{columns}",
            )


class SiteDropdownOverrideTests(UnfoldOverrideTestCase):
    """templates/unfold/helpers/site_dropdown.html is the company switcher."""

    def vendor_toggle_flag(self):
        header = (VENDOR_TEMPLATES / "unfold/helpers/navigation_header.html").read_text()
        match = re.search(r'x-on:click="(\w+) = !\1"', header)
        self.assertIsNotNone(
            match, "could not find the dropdown toggle in Unfold's navigation_header.html"
        )
        return match.group(1)

    def test_switcher_shows_on_the_flag_the_header_toggles(self):
        flag = self.vendor_toggle_flag()

        override = (PROJECT_TEMPLATES / "unfold/helpers/site_dropdown.html").read_text()
        self.assertIn(
            f'x-show="{flag}"',
            override,
            f"Unfold's header toggles `{flag}` but the site_dropdown.html override "
            "shows on a different variable, so the switcher would never open",
        )
        self.assertIn(f'x-on:click.outside="{flag} = false"', override)

        # The flag must exist in Unfold's root Alpine state or the header's
        # own toggle would throw at runtime.
        app_js = read_static("unfold/js/app.js")
        self.assertRegex(app_js, rf"\b{flag}\s*:", f"`{flag}` is not defined in unfold/js/app.js")

    def test_switcher_renders_one_checkbox_per_company_and_apply_form(self):
        response = self.client.get(reverse("admin:index"))
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()

        self.assertIn(f'x-show="{self.vendor_toggle_flag()}"', html)
        self.assertIn(
            f'action="{reverse("admin:company_switch_active_apply")}"', html
        )
        self.assertEqual(
            html.count('name="companies"'),
            2,
            "expected one checkbox per switchable company",
        )
        self.assertIn(f'value="{self.company_a.pk}"', html)
        self.assertIn(f'value="{self.company_b.pk}"', html)
        self.assertIn('name="csrfmiddlewaretoken"', html)
        # Middleware defaults a superuser to the first company by name, so its
        # checkbox starts checked and the other one does not.
        for company, checked in ((self.company_a, "true"), (self.company_b, "false")):
            self.assertIsNotNone(
                re.search(
                    rf'x-data="\{{ checked: {checked} \}}"[^>]*>\s*'
                    rf'<input type="checkbox" name="companies" value="{company.pk}"',
                    html,
                ),
                f"checkbox for {company.name} should start with checked: {checked}",
            )

    def test_logo_is_not_a_home_link_while_it_toggles_the_switcher(self):
        # site_logo.html override: with SITE_DROPDOWN configured the vendor
        # <a href="admin:index"> around the logo would navigate on every
        # click and close the dropdown before it opened.
        response = self.client.get(reverse("admin:index"))
        html = response.content.decode()
        index_url = re.escape(reverse("admin:index"))
        self.assertIsNone(
            re.search(rf'<a href="{index_url}">\s*<img', html),
            "the sidebar logo is wrapped in a link to admin:index again",
        )
        self.assertIsNotNone(
            re.search(r"<span>\s*<img[^>]*\bh-8\b", html),
            "the sidebar logo is no longer rendered inside a plain <span>",
        )


class VendorTemplateDriftTests(TestCase):
    """Fail loudly when Unfold changes a template an override was synced to."""

    def test_every_override_is_pinned(self):
        overrides = sorted(
            str(p.relative_to(PROJECT_TEMPLATES))
            for p in (PROJECT_TEMPLATES / "unfold").rglob("*.html")
        )
        unpinned = [o for o in overrides if o not in PINNED_VENDOR_HASHES]
        self.assertFalse(
            unpinned,
            "new Unfold template override(s) without a pinned vendor hash in "
            f"PINNED_VENDOR_HASHES: {unpinned}",
        )
        for override in overrides:
            self.assertTrue(
                (VENDOR_TEMPLATES / override).exists(),
                f"{override} no longer exists in Unfold; the override is dead code",
            )

    def test_vendor_templates_unchanged_since_overrides_were_synced(self):
        installed = importlib.metadata.version("django-unfold")
        changed = []
        for rel, pinned in PINNED_VENDOR_HASHES.items():
            path = VENDOR_TEMPLATES / rel
            if not path.exists():
                changed.append(f"{rel}: removed from Unfold")
                continue
            actual = hashlib.sha256(path.read_bytes()).hexdigest()
            if actual != pinned:
                changed.append(f"{rel}: now {actual}")

        self.assertFalse(
            changed,
            "Unfold vendor template(s) changed since the project overrides were "
            f"synced against django-unfold {PINNED_UNFOLD_VERSION} (installed: {installed}).\n"
            "For each file below, diff the vendor copy in the unfold package "
            "against the override in templates/unfold/helpers/ (navigation_header.html "
            "is not overridden but toggles the switcher flag), update the override "
            "if needed, then update the hash in PINNED_VENDOR_HASHES:\n  "
            + "\n  ".join(changed),
        )
