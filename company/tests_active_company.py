"""
company/tests_active_company.py

Tests for the single-active-company feature: the ActiveCompany service,
ActiveCompanyMiddleware, the admin switch view, and changelist scoping
through CompanyRestrictedAdmin.

Run just this file:
    python manage.py test company.tests_active_company
"""
import time

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse

from company.active_company import ActiveCompany
from company.models import CompanyUser, EmployeeDocumentType
from core.admin.company_access import get_user_companies
from api.fleet.tests.helpers import make_company, make_user

User = get_user_model()

SWITCH_URL_NAME = "admin:company_switch_active"
CHANGELIST_URL_NAME = "admin:company_employeedocumenttype_changelist"


class BaseActiveCompanyTestCase(TestCase):
    def setUp(self):
        cache.clear()  # LocMem cache persists across tests in-process
        self.company_a = make_company(name="Alpha")
        self.company_b = make_company(name="Beta")

    def make_member(self, username, *companies, staff=False):
        user = make_user(username=username)
        if staff:
            user.is_staff = True
            user.save(update_fields=["is_staff"])
        for company in companies:
            CompanyUser.objects.create(user=user, company=company, is_active=True)
        return user


class ActiveCompanyServiceTests(BaseActiveCompanyTestCase):
    def test_get_user_companies_narrows_to_holder(self):
        user = self.make_member("multi", self.company_a, self.company_b)
        try:
            ActiveCompany.set_current_id(self.company_b.pk)
            self.assertQuerySetEqual(
                get_user_companies(user), [self.company_b], ordered=False
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
            ActiveCompany.set_current_id(self.company_b.pk)  # not a member
            self.assertQuerySetEqual(get_user_companies(user), [])
        finally:
            ActiveCompany.clear_current()

    def test_switchable_for_superuser_active_only(self):
        superuser = User.objects.create_superuser("super", "s@example.com", "x")
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
        self.assertEqual(
            self.client.session.get(ActiveCompany.SESSION_KEY), self.company_a.pk
        )

    def test_multi_membership_defaults_to_first_by_name(self):
        user = self.make_member("multi", self.company_b, self.company_a, staff=True)
        self.client.force_login(user)
        self.client.get(reverse("admin:index"))
        self.assertEqual(
            self.client.session.get(ActiveCompany.SESSION_KEY), self.company_a.pk
        )

    def test_superuser_defaults_to_first_active_company(self):
        superuser = User.objects.create_superuser("super", "s@example.com", "x")
        self.client.force_login(superuser)
        self.client.get(reverse("admin:index"))
        self.assertEqual(
            self.client.session.get(ActiveCompany.SESSION_KEY), self.company_a.pk
        )

    def test_expired_selection_redefaults(self):
        user = self.make_member("multi", self.company_a, self.company_b, staff=True)
        self.client.force_login(user)
        session = self.client.session
        session[ActiveCompany.SESSION_KEY] = self.company_b.pk
        session[ActiveCompany.SESSION_SET_AT_KEY] = time.time() - (25 * 60 * 60)
        session.save()

        self.client.get(reverse("admin:index"))
        self.assertEqual(
            self.client.session.get(ActiveCompany.SESSION_KEY), self.company_a.pk
        )

    def test_unexpired_selection_is_kept(self):
        user = self.make_member("multi", self.company_a, self.company_b, staff=True)
        self.client.force_login(user)
        session = self.client.session
        session[ActiveCompany.SESSION_KEY] = self.company_b.pk
        session[ActiveCompany.SESSION_SET_AT_KEY] = time.time()
        session.save()

        self.client.get(reverse("admin:index"))
        self.assertEqual(
            self.client.session.get(ActiveCompany.SESSION_KEY), self.company_b.pk
        )

    def test_revoked_membership_redefaults(self):
        user = self.make_member("multi", self.company_a, self.company_b, staff=True)
        self.client.force_login(user)
        session = self.client.session
        session[ActiveCompany.SESSION_KEY] = self.company_b.pk
        session[ActiveCompany.SESSION_SET_AT_KEY] = time.time()
        session.save()

        CompanyUser.objects.filter(user=user, company=self.company_b).delete()
        cache.clear()  # skip the 5-min membership cache

        self.client.get(reverse("admin:index"))
        self.assertEqual(
            self.client.session.get(ActiveCompany.SESSION_KEY), self.company_a.pk
        )

    def test_zero_membership_user_has_no_active_company(self):
        user = self.make_member("orphan", staff=True)
        self.client.force_login(user)
        self.client.get(reverse("admin:index"))
        self.assertIsNone(self.client.session.get(ActiveCompany.SESSION_KEY))


class SwitchActiveCompanyViewTests(BaseActiveCompanyTestCase):
    def switch(self, company, next_url=None):
        data = {"company": company.pk}
        if next_url is not None:
            data["next"] = next_url
        return self.client.post(reverse(SWITCH_URL_NAME), data)

    def test_member_can_switch_between_their_companies(self):
        user = self.make_member("multi", self.company_a, self.company_b, staff=True)
        self.client.force_login(user)
        response = self.switch(self.company_b)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            self.client.session.get(ActiveCompany.SESSION_KEY), self.company_b.pk
        )

    def test_cannot_switch_to_foreign_company(self):
        user = self.make_member("solo", self.company_a, staff=True)
        self.client.force_login(user)
        self.client.get(reverse("admin:index"))  # establish default (A)
        response = self.switch(self.company_b)
        self.assertEqual(response.status_code, 302)  # form_invalid -> admin index
        self.assertEqual(
            self.client.session.get(ActiveCompany.SESSION_KEY), self.company_a.pk
        )

    def test_superuser_can_switch_to_any_active_company(self):
        superuser = User.objects.create_superuser("super", "s@example.com", "x")
        self.client.force_login(superuser)
        response = self.switch(self.company_b)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            self.client.session.get(ActiveCompany.SESSION_KEY), self.company_b.pk
        )

    def test_safe_next_redirect(self):
        user = self.make_member("multi", self.company_a, self.company_b, staff=True)
        self.client.force_login(user)
        response = self.switch(self.company_b, next_url="/admin/company/company/")
        self.assertEqual(response["Location"], "/admin/company/company/")

    def test_unsafe_next_falls_back_to_admin_index(self):
        user = self.make_member("multi", self.company_a, self.company_b, staff=True)
        self.client.force_login(user)
        response = self.switch(self.company_b, next_url="https://evil.example.com/")
        self.assertEqual(response["Location"], reverse("admin:index"))

    def test_get_not_allowed(self):
        user = self.make_member("solo", self.company_a, staff=True)
        self.client.force_login(user)
        response = self.client.get(reverse(SWITCH_URL_NAME))
        self.assertEqual(response.status_code, 405)

    def test_non_staff_redirected_to_login(self):
        user = self.make_member("plain", self.company_a, self.company_b)  # not staff
        self.client.force_login(user)
        self.client.get("/")  # middleware defaults to Alpha
        response = self.switch(self.company_b)
        self.assertEqual(response.status_code, 302)
        self.assertIn("login", response["Location"])
        # The switch was rejected: the default (Alpha) is untouched.
        self.assertEqual(
            self.client.session.get(ActiveCompany.SESSION_KEY), self.company_a.pk
        )


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

    def changelist_objects(self):
        response = self.client.get(reverse(CHANGELIST_URL_NAME))
        self.assertEqual(response.status_code, 200)
        return list(response.context["cl"].queryset)

    def test_member_sees_only_active_company_rows(self):
        user = self.make_member("multi", self.company_a, self.company_b, staff=True)
        user.user_permissions.add(
            Permission.objects.get(codename="view_employeedocumenttype")
        )
        self.client.force_login(user)

        self.assertEqual(self.changelist_objects(), [self.doc_a])  # default: Alpha

        self.client.post(
            reverse(SWITCH_URL_NAME), {"company": self.company_b.pk}
        )
        self.assertEqual(self.changelist_objects(), [self.doc_b])

    def test_superuser_scoped_but_sees_null_company_rows(self):
        superuser = User.objects.create_superuser("super", "s@example.com", "x")
        self.client.force_login(superuser)

        objects = self.changelist_objects()  # default: Alpha
        self.assertIn(self.doc_a, objects)
        self.assertIn(self.doc_orphan, objects)
        self.assertNotIn(self.doc_b, objects)

    def test_company_admin_scoped_to_active_company_for_superuser(self):
        """The Company changelist follows the switcher too: one company at a
        time, even for superusers. Other companies become visible by switching."""
        superuser = User.objects.create_superuser("super", "s@example.com", "x")
        self.client.force_login(superuser)

        def listed_companies():
            response = self.client.get(reverse("admin:company_company_changelist"))
            self.assertEqual(response.status_code, 200)
            return list(response.context["cl"].queryset)

        self.assertEqual(listed_companies(), [self.company_a])  # default: Alpha

        self.client.post(
            reverse(SWITCH_URL_NAME), {"company": self.company_b.pk}
        )
        self.assertEqual(listed_companies(), [self.company_b])
