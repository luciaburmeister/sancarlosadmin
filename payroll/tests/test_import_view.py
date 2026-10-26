"""Admin import view: choosing the company when several share a NIF."""

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse

from payroll.models import PayrollPeriod
from payroll.tests.factories import make_company, make_importer_employees
from payroll.tests.test_a3_importer import build_a3_workbook

IMPORT_URL = "admin:payroll_payroll_import"


class SharedNifImportViewTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.fislav = make_company(name="FISLAV")
        cls.fismec = make_company(name="FISMEC")
        make_importer_employees(company=cls.fislav)
        make_importer_employees(company=cls.fismec)
        cls.user = get_user_model().objects.create_user(
            username="payroll-importer",
            password="x",
            is_staff=True,
            is_superuser=True,
        )

    def setUp(self):
        self.client.force_login(self.user)

    def _upload(self, **extra):
        upload = SimpleUploadedFile("march.xlsx", build_a3_workbook().getvalue())
        return self.client.post(reverse(IMPORT_URL), {"file": upload, **extra})

    def test_shared_nif_asks_to_pick_one_of_the_two_companies(self):
        response = self._upload()

        self.assertEqual(response.status_code, 200)
        report = response.context["report"]
        self.assertTrue(report.needs_company_selection)
        self.assertFalse(report.can_commit)
        ids = set(
            response.context["company_form"].fields["company"].queryset.values_list(
                "pk", flat=True
            )
        )
        self.assertEqual(ids, {self.fislav.pk, self.fismec.pk})

        html = response.content.decode()
        self.assertLess(html.find('value="revalidate"'), html.find("GARCIA"))
        self.assertIn("FISLAV (B12345678)", html)
        self.assertIn("FISMEC (B12345678)", html)
        self.assertNotContains(response, "Resuelve los errores bloqueantes")

    def test_picking_a_company_on_upload_imports_into_that_company(self):
        response = self._upload(company=self.fismec.pk)

        self.assertEqual(response.status_code, 200)
        report = response.context["report"]
        self.assertFalse(report.needs_company_selection)
        self.assertTrue(report.can_commit, report.errors)
        self.assertEqual(report.company_id, self.fismec.pk)
        self.assertEqual(response.context["selected_company_id"], self.fismec.pk)
        self.assertContains(response, f'name="company_id" value="{self.fismec.pk}"')

        confirm = self.client.post(
            reverse(IMPORT_URL),
            {
                "action": "confirm",
                "token": response.context["token"],
                "source_file": "march.xlsx",
                "company_id": self.fismec.pk,
            },
        )
        self.assertEqual(confirm.status_code, 302)
        period = PayrollPeriod.objects.get()
        self.assertEqual(period.company, self.fismec)
        self.assertEqual(period.payrolls.count(), 2)

    def test_revalidate_then_confirm_uses_the_selected_company(self):
        preview = self._upload()
        token = preview.context["token"]

        chosen = self.client.post(
            reverse(IMPORT_URL),
            {
                "action": "revalidate",
                "token": token,
                "source_file": "march.xlsx",
                "company": self.fislav.pk,
            },
        )
        self.assertEqual(chosen.status_code, 200)
        self.assertEqual(chosen.context["report"].company_id, self.fislav.pk)
        self.assertTrue(chosen.context["report"].can_commit)

        confirm = self.client.post(
            reverse(IMPORT_URL),
            {
                "action": "confirm",
                "token": token,
                "source_file": "march.xlsx",
                "company_id": self.fislav.pk,
            },
        )
        self.assertEqual(confirm.status_code, 302)
        self.assertEqual(PayrollPeriod.objects.get().company, self.fislav)
