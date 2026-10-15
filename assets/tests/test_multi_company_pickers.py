"""
assets/tests/test_multi_company_pickers.py

Regression test for a bug where a user with access to more than one
company (via CompanyUser, not just their "default" Employee company)
could pick a project file/asset/driver/truck through the autocomplete
search (restricted by CompanyRestrictedAdmin to *every* company they
have access to) that then failed validation on save, because
AssetPlanningRequestAdmin.formfield_for_foreignkey/manytomany narrowed
the field's queryset to a single "target" company.

Run just this file:
    python manage.py test assets.tests.test_multi_company_pickers
"""
from django.contrib.auth import get_user_model
from django.test import RequestFactory, TestCase

from assets.admin.asset_planning_request import AssetPlanningRequestAdmin
from assets.models import AssetPlanningRequest
from assets.tests.factories import make_project_file, make_truck, make_truck_type
from company.models import CompanyUser
from api.fleet.tests.helpers import make_company, make_employee, make_user

User = get_user_model()


class MultiCompanyPickerTests(TestCase):
    def setUp(self):
        self.company_a = make_company(name="Company A")
        self.company_b = make_company(name="Company B")

        self.user = make_user(username="multi_company_user")
        # Employee's "default" company is A ...
        make_employee(self.company_a, self.user)
        # ... but the user is also granted access to B.
        CompanyUser.objects.create(user=self.user, company=self.company_a, is_active=True)
        CompanyUser.objects.create(user=self.user, company=self.company_b, is_active=True)

        self.project_file_b = make_project_file(self.company_b, name="Project B", client_name="Client B")
        self.truck_b = make_truck(
            self.company_b, name="Truck B", asset_type=make_truck_type(self.company_b, name="Camion"),
        )

        self.admin = AssetPlanningRequestAdmin(AssetPlanningRequest, admin_site=None)
        self.request = RequestFactory().get("/")
        self.request.user = self.user
        self.request._obj_ = None

    def test_project_file_queryset_includes_non_default_company(self):
        db_field = AssetPlanningRequest._meta.get_field("project_file")
        formfield = self.admin.formfield_for_foreignkey(db_field, self.request)
        self.assertIn(self.project_file_b, formfield.queryset)

    def test_suggested_trucks_queryset_includes_non_default_company(self):
        db_field = AssetPlanningRequest._meta.get_field("suggested_trucks")
        formfield = self.admin.formfield_for_manytomany(db_field, self.request)
        self.assertIn(self.truck_b, formfield.queryset)
