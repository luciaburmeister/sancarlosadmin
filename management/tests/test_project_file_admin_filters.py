"""Invoicing and created-by filters on the ProjectFile changelist."""
from decimal import Decimal

from django.contrib.admin.sites import site
from django.contrib.auth import get_user_model
from django.test import RequestFactory, TestCase

from company.active_company import ActiveCompany
from company.models import Company, CompanyUser
from management.admin.filters import ProjectFileCreatedByFilter, ProjectFileInvoicingFilter
from management.admin.project_file import ProjectFileAdmin
from management.models import ProjectFile
from sales.models import Client


class ProjectFileAdminFilterTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.company = Company.objects.create(name="Own Co")
        cls.other_company = Company.objects.create(name="Other Co")
        cls.viewer = User.objects.create_user(username="viewer", is_staff=True)
        CompanyUser.objects.create(user=cls.viewer, company=cls.company)
        cls.ana = User.objects.create_user(username="ana", first_name="Ana", last_name="Ruiz")
        cls.outsider = User.objects.create_user(username="outsider")

        client = Client.objects.create(name="Acme", company=cls.company)
        other_client = Client.objects.create(name="Beta", company=cls.other_company)

        def pf(name, total, invoiced, pct, created_by=None, company=cls.company, client=client,
               advance_pending="0"):
            obj = ProjectFile.objects.create(
                name=name, company=company, client=client, created_by=created_by,
            )
            # Bypass save()/signals so the cached totals stay as set here.
            ProjectFile.objects.filter(pk=obj.pk).update(
                total_amount=Decimal(total),
                total_invoiced=Decimal(invoiced),
                percentage_invoiced=Decimal(pct),
                advance_pending_amount=Decimal(advance_pending),
            )
            return obj

        pf("empty", "0", "0", "0")
        pf("pending", "100", "0", "0", created_by=cls.ana)
        pf("partial", "100", "40", "40", created_by=cls.ana)
        pf("done", "100", "100", "100")
        # A 100% advance still to deduct is not fully invoiced yet.
        pf("advance", "100", "100", "100", advance_pending="100")
        pf("foreign", "100", "0", "0", created_by=cls.outsider,
           company=cls.other_company, client=other_client)

    def setUp(self):
        self.admin = ProjectFileAdmin(ProjectFile, site)
        ActiveCompany.set_current_ids([self.company.pk])
        self.addCleanup(ActiveCompany.clear_current)

    def _names(self, filter_class, params):
        request = RequestFactory().get("/", params)
        request.user = self.viewer
        queryset = self.admin.get_queryset(request)
        list_filter = filter_class(request, request.GET.copy(), ProjectFile, self.admin)
        return sorted(list_filter.queryset(request, queryset).values_list("name", flat=True)), list_filter

    def test_invoicing_filter(self):
        cases = {
            "not_invoiced": ["empty", "pending"],
            "partially_invoiced": ["advance", "partial"],
            "fully_invoiced": ["done"],
        }
        for value, expected in cases.items():
            with self.subTest(value=value):
                names, _ = self._names(ProjectFileInvoicingFilter, {"invoicing": value})
                self.assertEqual(names, expected)

    def test_created_by_filter_lists_no_users(self):
        request = RequestFactory().get("/")
        request.user = self.viewer
        list_filter = ProjectFileCreatedByFilter(request, {}, ProjectFile, self.admin)
        self.assertEqual(list(list_filter.lookup_choices), [])
        self.assertTrue(list_filter.has_output())

    def test_created_by_filter_matches_typed_username(self):
        names, _ = self._names(ProjectFileCreatedByFilter, {"created_by": " ANA "})
        self.assertEqual(names, ["partial", "pending"])

        names, _ = self._names(ProjectFileCreatedByFilter, {"created_by": "an"})
        self.assertEqual(names, [])

    def test_created_by_filter_stays_company_scoped(self):
        names, _ = self._names(ProjectFileCreatedByFilter, {"created_by": "outsider"})
        self.assertEqual(names, [])

    def test_admin_add_records_creator(self):
        request = RequestFactory().post("/")
        request.user = self.viewer
        obj = ProjectFile(name="new", company=self.company, client=Client.objects.get(name="Acme"))
        self.admin.save_model(request, obj, form=None, change=False)
        self.assertEqual(obj.created_by, self.viewer)
