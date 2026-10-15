"""
company/tests_active_company.py

Tests for the active-companies feature: the ActiveCompany service,
ActiveCompanyMiddleware, the admin toggle view, and changelist scoping
through CompanyRestrictedAdmin. Several companies can be active at once;
the switcher toggles each one in or out of the set.

Run just this file:
    python manage.py test company.tests_active_company
"""
import time

from django.contrib import admin
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.cache import cache
from django.test import RequestFactory, TestCase
from django.urls import reverse

from company.active_company import ActiveCompany
from company.models import CompanyUser, EmployeeDocumentType
from core.admin.company_access import get_user_companies
from core.admin.company_admin import CompanyRestrictedAdmin
from api.fleet.tests.helpers import make_company, make_user

User = get_user_model()

SWITCH_URL_NAME = "admin:company_switch_active"
APPLY_URL_NAME = "admin:company_switch_active_apply"
CHANGELIST_URL_NAME = "admin:company_employeedocumenttype_changelist"


class BaseActiveCompanyTestCase(TestCase):
    def setUp(self):
        cache.clear()  # LocMem cache persists across tests in-process
        self.company_a = make_company(name="Alpha")
        self.company_b = make_company(name="Beta")

    def make_superuser(self):
        """Create the superuser these tests log in as."""
        return User.objects.create_superuser("super", "s@example.com", "x")

    def make_member(self, username, *companies, staff=False):
        user = make_user(username=username)
        if staff:
            user.is_staff = True
            user.save(update_fields=["is_staff"])
        for company in companies:
            CompanyUser.objects.create(user=user, company=company, is_active=True)
        return user

    def active_ids(self):
        return self.client.session.get(ActiveCompany.SESSION_KEY)

    def toggle(self, company, next_url=None):
        data = {"company": company.pk}
        if next_url is not None:
            data["next"] = next_url
        return self.client.post(reverse(SWITCH_URL_NAME), data)

    def apply(self, *companies, next_url=None):
        data = {"companies": [c.pk for c in companies]}
        if next_url is not None:
            data["next"] = next_url
        return self.client.post(reverse(APPLY_URL_NAME), data)


class ActiveCompanyServiceTests(BaseActiveCompanyTestCase):
    def test_get_user_companies_narrows_to_holder(self):
        user = self.make_member("multi", self.company_a, self.company_b)
        try:
            ActiveCompany.set_current_ids([self.company_b.pk])
            self.assertQuerySetEqual(
                get_user_companies(user), [self.company_b], ordered=False
            )
        finally:
            ActiveCompany.clear_current()

    def test_get_user_companies_with_several_active(self):
        company_c = make_company(name="Gamma")
        user = self.make_member("multi", self.company_a, self.company_b, company_c)
        try:
            ActiveCompany.set_current_ids([self.company_a.pk, company_c.pk])
            self.assertQuerySetEqual(
                get_user_companies(user),
                [self.company_a, company_c],
                ordered=False,
            )
        finally:
            ActiveCompany.clear_current()

    def test_get_user_companies_full_list_without_holder(self):
        """Celery/shell behavior: no holder -> full allowed list."""
        user = self.make_member("multi", self.company_a, self.company_b)
        self.assertQuerySetEqual(
            get_user_companies(user),
            [self.company_a, self.company_b],
            ordered=False,
        )

    def test_holder_cannot_widen_access(self):
        user = self.make_member("single", self.company_a)
        try:
            ActiveCompany.set_current_ids([self.company_b.pk])  # not a member
            self.assertQuerySetEqual(get_user_companies(user), [])
        finally:
            ActiveCompany.clear_current()

    def test_switchable_for_superuser_active_only(self):
        superuser = self.make_superuser()
        inactive = make_company(name="Closed")
        inactive.is_active = False
        inactive.save(update_fields=["is_active"])
        switchable = ActiveCompany.switchable_for(superuser)
        self.assertIn(self.company_a, switchable)
        self.assertNotIn(inactive, switchable)


class ActiveCompanyMiddlewareTests(BaseActiveCompanyTestCase):
    def test_single_membership_defaults_to_that_company(self):
        user = self.make_member("solo", self.company_a, staff=True)
        self.client.force_login(user)
        self.client.get(reverse("admin:index"))
        self.assertEqual(self.active_ids(), [self.company_a.pk])

    def test_multi_membership_defaults_to_first_by_name(self):
        user = self.make_member("multi", self.company_b, self.company_a, staff=True)
        self.client.force_login(user)
        self.client.get(reverse("admin:index"))
        self.assertEqual(self.active_ids(), [self.company_a.pk])

    def test_superuser_defaults_to_first_active_company(self):
        superuser = self.make_superuser()
        self.client.force_login(superuser)
        self.client.get(reverse("admin:index"))
        self.assertEqual(self.active_ids(), [self.company_a.pk])

    def test_expired_selection_redefaults(self):
        user = self.make_member("multi", self.company_a, self.company_b, staff=True)
        self.client.force_login(user)
        session = self.client.session
        session[ActiveCompany.SESSION_KEY] = [self.company_b.pk]
        session[ActiveCompany.SESSION_SET_AT_KEY] = time.time() - (25 * 60 * 60)
        session.save()

        self.client.get(reverse("admin:index"))
        self.assertEqual(self.active_ids(), [self.company_a.pk])

    def test_unexpired_selection_is_kept(self):
        user = self.make_member("multi", self.company_a, self.company_b, staff=True)
        self.client.force_login(user)
        session = self.client.session
        session[ActiveCompany.SESSION_KEY] = [self.company_b.pk, self.company_a.pk]
        session[ActiveCompany.SESSION_SET_AT_KEY] = time.time()
        session.save()

        response = self.client.get(reverse("admin:index"))
        self.assertCountEqual(self.active_ids(), [self.company_a.pk, self.company_b.pk])
        self.assertEqual(
            [c.pk for c in response.wsgi_request.active_companies],
            [self.company_a.pk, self.company_b.pk],  # sorted by name
        )

    def test_legacy_single_id_session_value_is_upgraded(self):
        """Sessions written before the multi-company switcher stored an int."""
        user = self.make_member("multi", self.company_a, self.company_b, staff=True)
        self.client.force_login(user)
        session = self.client.session
        session[ActiveCompany.SESSION_KEY] = self.company_b.pk
        session[ActiveCompany.SESSION_SET_AT_KEY] = time.time()
        session.save()

        self.client.get(reverse("admin:index"))
        self.assertEqual(self.active_ids(), [self.company_b.pk])

    def test_revoked_membership_is_dropped_but_rest_survive(self):
        user = self.make_member("multi", self.company_a, self.company_b, staff=True)
        self.client.force_login(user)
        session = self.client.session
        session[ActiveCompany.SESSION_KEY] = [self.company_a.pk, self.company_b.pk]
        session[ActiveCompany.SESSION_SET_AT_KEY] = time.time()
        session.save()

        CompanyUser.objects.filter(user=user, company=self.company_b).delete()
        cache.clear()  # skip the 5-min membership cache

        self.client.get(reverse("admin:index"))
        self.assertEqual(self.active_ids(), [self.company_a.pk])

    def test_revoked_only_membership_redefaults(self):
        user = self.make_member("multi", self.company_a, self.company_b, staff=True)
        self.client.force_login(user)
        session = self.client.session
        session[ActiveCompany.SESSION_KEY] = [self.company_b.pk]
        session[ActiveCompany.SESSION_SET_AT_KEY] = time.time()
        session.save()

        CompanyUser.objects.filter(user=user, company=self.company_b).delete()
        cache.clear()

        self.client.get(reverse("admin:index"))
        self.assertEqual(self.active_ids(), [self.company_a.pk])

    def test_zero_membership_user_has_no_active_company(self):
        user = self.make_member("orphan", staff=True)
        self.client.force_login(user)
        self.client.get(reverse("admin:index"))
        self.assertIsNone(self.active_ids())


class SwitchActiveCompanyViewTests(BaseActiveCompanyTestCase):
    """The toggle view (one company in or out) and the dropdown's "Apply"
    view share the same mixin; the checks common to both run as subTests."""

    def both_views(self):
        return (("toggle", self.toggle), ("apply", self.apply))

    def test_clicking_inactive_company_adds_it(self):
        user = self.make_member("multi", self.company_a, self.company_b, staff=True)
        self.client.force_login(user)
        self.client.get(reverse("admin:index"))  # default: Alpha
        response = self.toggle(self.company_b)
        self.assertEqual(response.status_code, 302)
        self.assertCountEqual(self.active_ids(), [self.company_a.pk, self.company_b.pk])

    def test_clicking_active_company_removes_it(self):
        user = self.make_member("multi", self.company_a, self.company_b, staff=True)
        self.client.force_login(user)
        self.client.get(reverse("admin:index"))
        self.toggle(self.company_b)  # add
        self.toggle(self.company_a)  # remove
        self.assertEqual(self.active_ids(), [self.company_b.pk])

    def test_cannot_remove_last_active_company(self):
        user = self.make_member("multi", self.company_a, self.company_b, staff=True)
        self.client.force_login(user)
        self.client.get(reverse("admin:index"))  # default: Alpha
        response = self.toggle(self.company_a)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.active_ids(), [self.company_a.pk])

    def test_cannot_select_foreign_company(self):
        user = self.make_member("solo", self.company_a, staff=True)
        self.client.force_login(user)
        self.client.get(reverse("admin:index"))  # establish default (A)
        for view, post in self.both_views():
            with self.subTest(view=view):
                response = post(self.company_b)  # not a member
                self.assertEqual(response.status_code, 302)  # form_invalid -> admin index
                self.assertEqual(self.active_ids(), [self.company_a.pk])

    def test_superuser_can_add_any_active_company(self):
        superuser = self.make_superuser()
        self.client.force_login(superuser)
        self.client.get(reverse("admin:index"))
        response = self.toggle(self.company_b)
        self.assertEqual(response.status_code, 302)
        self.assertCountEqual(self.active_ids(), [self.company_a.pk, self.company_b.pk])

    def test_safe_next_redirect(self):
        user = self.make_member("multi", self.company_a, self.company_b, staff=True)
        self.client.force_login(user)
        for view, post in self.both_views():
            with self.subTest(view=view):
                response = post(self.company_b, next_url="/admin/company/company/")
                self.assertEqual(response["Location"], "/admin/company/company/")

    def test_unsafe_next_falls_back_to_admin_index(self):
        user = self.make_member("multi", self.company_a, self.company_b, staff=True)
        self.client.force_login(user)
        for view, post in self.both_views():
            with self.subTest(view=view):
                response = post(self.company_b, next_url="https://evil.example.com/")
                self.assertEqual(response["Location"], reverse("admin:index"))

    def test_get_not_allowed(self):
        user = self.make_member("solo", self.company_a, staff=True)
        self.client.force_login(user)
        for url_name in (SWITCH_URL_NAME, APPLY_URL_NAME):
            with self.subTest(url_name=url_name):
                response = self.client.get(reverse(url_name))
                self.assertEqual(response.status_code, 405)

    def test_non_staff_redirected_to_login(self):
        user = self.make_member("plain", self.company_a, self.company_b)  # not staff
        self.client.force_login(user)
        self.client.get("/")  # middleware defaults to Alpha
        response = self.toggle(self.company_b)
        self.assertEqual(response.status_code, 302)
        self.assertIn("login", response["Location"])
        # The toggle was rejected: the default (Alpha) is untouched.
        self.assertEqual(self.active_ids(), [self.company_a.pk])


class SetActiveCompaniesViewTests(BaseActiveCompanyTestCase):
    """The dropdown's "Apply" button: replaces the active set in one request.
    Checks it shares with the toggle view live in SwitchActiveCompanyViewTests."""

    def test_apply_replaces_active_set(self):
        user = self.make_member("multi", self.company_a, self.company_b, staff=True)
        self.client.force_login(user)
        self.client.get(reverse("admin:index"))  # default: Alpha

        response = self.apply(self.company_b)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.active_ids(), [self.company_b.pk])

    def test_apply_can_select_several_at_once(self):
        company_c = make_company(name="Gamma")
        user = self.make_member(
            "multi", self.company_a, self.company_b, company_c, staff=True
        )
        self.client.force_login(user)
        self.client.get(reverse("admin:index"))  # default: Alpha

        response = self.apply(self.company_b, company_c)
        self.assertEqual(response.status_code, 302)
        self.assertCountEqual(
            self.active_ids(), [self.company_b.pk, company_c.pk]
        )

    def test_apply_with_no_companies_is_rejected(self):
        user = self.make_member("multi", self.company_a, self.company_b, staff=True)
        self.client.force_login(user)
        self.client.get(reverse("admin:index"))  # default: Alpha

        response = self.client.post(reverse(APPLY_URL_NAME), {})
        self.assertEqual(response.status_code, 302)  # form_invalid -> admin index
        self.assertEqual(self.active_ids(), [self.company_a.pk])  # untouched


class ChangelistScopingTests(BaseActiveCompanyTestCase):
    def setUp(self):
        super().setUp()
        self.doc_a = EmployeeDocumentType.objects.create(
            name="Doc A", company=self.company_a
        )
        self.doc_b = EmployeeDocumentType.objects.create(
            name="Doc B", company=self.company_b
        )
        self.doc_orphan = EmployeeDocumentType.objects.create(
            name="Doc Orphan", company=None
        )

    def changelist(self):
        response = self.client.get(reverse(CHANGELIST_URL_NAME))
        self.assertEqual(response.status_code, 200)
        return response.context["cl"]

    def changelist_objects(self):
        return list(self.changelist().queryset)

    def test_member_sees_rows_of_every_active_company(self):
        user = self.make_member("multi", self.company_a, self.company_b, staff=True)
        user.user_permissions.add(
            Permission.objects.get(codename="view_employeedocumenttype")
        )
        self.client.force_login(user)

        self.assertEqual(self.changelist_objects(), [self.doc_a])  # default: Alpha

        self.toggle(self.company_b)  # add Beta
        self.assertCountEqual(self.changelist_objects(), [self.doc_a, self.doc_b])

        self.toggle(self.company_a)  # remove Alpha
        self.assertEqual(self.changelist_objects(), [self.doc_b])

    def test_company_column_appended_only_with_several_active(self):
        class DocAdmin(CompanyRestrictedAdmin):
            list_display = ("name",)
            list_filter = ("name", "company")

        model_admin = DocAdmin(EmployeeDocumentType, admin.site)
        request = RequestFactory().get("/")
        try:
            ActiveCompany.set_current_ids([self.company_a.pk])
            self.assertEqual(model_admin.get_list_display(request), ["name"])
            self.assertEqual(model_admin.get_list_filter(request), ["name"])

            ActiveCompany.set_current_ids([self.company_a.pk, self.company_b.pk])
            self.assertEqual(model_admin.get_list_display(request), ["name", "company"])
            self.assertEqual(
                list(model_admin.get_list_filter(request)), ["name", "company"]
            )
        finally:
            ActiveCompany.clear_current()

    def test_existing_company_column_is_not_duplicated(self):
        """EmployeeDocumentTypeAdmin already lists ``company``."""
        user = self.make_member("multi", self.company_a, self.company_b, staff=True)
        user.user_permissions.add(
            Permission.objects.get(codename="view_employeedocumenttype")
        )
        self.client.force_login(user)
        self.toggle(self.company_b)
        self.assertEqual(list(self.changelist().list_display).count("company"), 1)

    def test_superuser_scoped_but_sees_null_company_rows(self):
        superuser = self.make_superuser()
        self.client.force_login(superuser)

        objects = self.changelist_objects()  # default: Alpha
        self.assertIn(self.doc_a, objects)
        self.assertIn(self.doc_orphan, objects)
        self.assertNotIn(self.doc_b, objects)

    def test_company_admin_follows_active_set_for_superuser(self):
        """The Company changelist follows the switcher too, even for
        superusers. Other companies become visible by adding them."""
        superuser = self.make_superuser()
        self.client.force_login(superuser)

        def listed_companies():
            response = self.client.get(reverse("admin:company_company_changelist"))
            self.assertEqual(response.status_code, 200)
            return list(response.context["cl"].queryset)

        self.assertEqual(listed_companies(), [self.company_a])  # default: Alpha

        self.toggle(self.company_b)
        self.assertCountEqual(listed_companies(), [self.company_a, self.company_b])
