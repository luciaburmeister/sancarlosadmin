"""A new company's setup: the shell staff create, and the steps its admin completes."""
from io import BytesIO

import openpyxl
import tablib
from django.contrib.auth import get_user_model
from django.core import mail
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse
from django.utils import translation

from accounting.models import (
    AccountRole, BusinessUnit, ChartOfAccountsTemplate, CompanyAccount, FiscalYear, TemplateAccount,
)
from accounting.services.setup_check import AccountingSetupCheck
from company.models import BankAccount, Company, CompanyModule, CompanyUser, ConsentStatus, Employee
from company.services.onboarding import COMPANY_ADMIN_GROUP, company_admin_group, create_company_shell, invite_user
from general.onboarding import MISSING, OK, WARNING, CompanyReadiness
from invoices.models import Invoice, SalesInvoice
from inventory.models import Warehouse
from invoices.tests.factories import get_iva21, make_company

User = get_user_model()

GIF = (
    b"GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\xff\xff\xff!\xf9\x04\x01\x00\x00\x00\x00"
    b",\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02D\x01\x00;"
)


def logo():
    return SimpleUploadedFile("logo.gif", GIF, content_type="image/gif")


def pgc():
    """A chart template with the one account every company needs a role on: sales of goods."""
    template, created = ChartOfAccountsTemplate.objects.get_or_create(
        name="PGC test", defaults={"country": "ES", "is_default": True},
    )
    if created:
        group = TemplateAccount.objects.create(template=template, code="70", name="Ventas", is_postable=False)
        TemplateAccount.objects.create(
            template=template, code="700", name="Ventas de mercaderías", parent=group,
            role=AccountRole.GOODS_SALES,
        )
    return template


def make_shell(test, *, modules=(), email="admin@newco.es"):
    with test.captureOnCommitCallbacks(execute=True):
        return create_company_shell(
            name="NewCo", legal_name="NewCo SL", tax_identifier="B99999999", logo=logo(),
            constituted_country="ES", chart_template=pgc(), modules=modules,
            admin_email=email, admin_first_name="Ana", admin_last_name="López",
            base_url="https://erp.example.com/",
        )


class CreateCompanyShellTest(TestCase):
    def test_company_has_what_every_company_needs(self):
        company = make_shell(self)

        self.assertTrue(CompanyAccount.objects.filter(company=company, role=AccountRole.GOODS_SALES).exists())
        self.assertTrue(BusinessUnit.objects.filter(company=company, is_default=True).exists())
        self.assertTrue(FiscalYear.objects.filter(company=company).exists())
        self.assertIsNone(company.onboarding_completed_at)
        self.assertTrue(AccountingSetupCheck(company).is_ready())

    def test_admin_is_invited_as_staff_employee_with_the_setup_permission(self):
        company = make_shell(self)

        user = User.objects.get(email="admin@newco.es")
        self.assertTrue(user.is_staff)
        self.assertFalse(user.has_usable_password())
        self.assertTrue(CompanyUser.objects.filter(user=user, company=company, is_active=True).exists())
        self.assertEqual(Employee.objects.get(user=user).company, company)
        self.assertTrue(user.has_perm("company.can_onboard_company"))
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["admin@newco.es"])
        self.assertIn("https://erp.example.com/", mail.outbox[0].body)

    def test_existing_user_keeps_their_password_and_employee(self):
        other = make_company(name="Other")
        user = User.objects.create_user("ana", email="ana@other.es", password="secret")
        Employee.objects.create(company=other, user=user, first_name="Ana", last_name="L", hired_at="2026-01-01")
        company = make_company(name="Mine")

        with self.captureOnCommitCallbacks(execute=True):
            invite_user(company, email="ANA@other.es", first_name="Ana", last_name="L",
                        base_url="https://erp.example.com")

        user.refresh_from_db()
        self.assertTrue(user.check_password("secret"))
        self.assertEqual(Employee.objects.get(user=user).company, other)
        self.assertTrue(CompanyUser.objects.filter(user=user, company=company).exists())
        self.assertIn(reverse("admin:index"), mail.outbox[0].body)

    def test_company_admin_group_permissions_all_exist(self):
        group = company_admin_group()

        self.assertEqual(group.name, COMPANY_ADMIN_GROUP)
        self.assertTrue(group.permissions.filter(codename="can_onboard_company").exists())


class CompanyReadinessTest(TestCase):
    def test_steps_of_unused_modules_are_hidden(self):
        company = make_company(enabled_modules=[CompanyModule.INVENTORY])

        keys = [step.key for step in CompanyReadiness(company).steps]

        self.assertIn("inventory", keys)
        self.assertNotIn("production", keys)
        self.assertNotIn("einvoicing", keys)

    def test_an_empty_company_is_not_ready(self):
        readiness = CompanyReadiness(make_company())

        self.assertFalse(readiness.is_ready())
        self.assertEqual(readiness.step("details").status, MISSING)

    def test_missing_items_in_optional_steps_dont_block(self):
        readiness = CompanyReadiness(make_company())

        self.assertFalse(any(step.blocks_finish for step in readiness.steps if not step.required))


class BankAccountsStepTest(TestCase):
    def setUp(self):
        self.company = make_company(name="NewCo")
        self.step = CompanyReadiness(self.company).step("bank")

    def check(self):
        return self.step.sections()[0].checks[0]

    def test_the_main_action_connects_the_bank_for_this_company(self):
        (label, url, icon), = self.step.actions()

        self.assertTrue(url.startswith(reverse("admin:banking_connect_bank")))
        self.assertIn(f"company_id={self.company.pk}", url)

    def test_no_account_is_missing(self):
        self.assertEqual(self.check().status, MISSING)

    def test_an_account_entered_by_hand_asks_to_be_connected(self):
        BankAccount.objects.create(company=self.company, bank_name="Banco Uno", iban="ES00", is_default=True)

        self.assertEqual(self.check().status, WARNING)
        self.assertIn(reverse("admin:banking_connect_bank"), self.check().url)

    def test_a_connected_account_is_ok(self):
        BankAccount.objects.create(
            company=self.company, bank_name="Banco Uno", iban="ES00", is_default=True, nuek_token="tok",
        )

        self.assertEqual(self.check().status, OK)

    def test_an_expired_connection_links_to_reconnect_that_account(self):
        account = BankAccount.objects.create(
            company=self.company, bank_name="Banco Uno", is_default=True, nuek_token="tok",
            consent_status=ConsentStatus.INVALID,
        )

        self.assertEqual(self.check().status, WARNING)
        self.assertIn(f"bank_account_id={account.pk}", self.check().url)


class OnboardingAccessTest(TestCase):
    def setUp(self):
        self.company = make_shell(self)
        self.user = User.objects.get(email="admin@newco.es")
        self.client.force_login(self.user)

    def test_admin_cannot_open_another_companys_setup(self):
        other = make_company(name="Other")

        response = self.client.get(reverse("admin:company_company_onboarding", args=[other.pk]))

        self.assertEqual(response.status_code, 404)

    def test_member_without_the_permission_is_refused(self):
        with self.captureOnCommitCallbacks(execute=True):
            member = invite_user(self.company, email="worker@newco.es", first_name="W", last_name="W",
                                 base_url="https://erp.example.com")
        self.client.force_login(member)

        response = self.client.get(reverse("admin:company_company_onboarding", args=[self.company.pk]))

        self.assertEqual(response.status_code, 403)

    def test_only_superusers_create_companies(self):
        response = self.client.get(reverse("admin:company_company_onboarding_new"))

        self.assertEqual(response.status_code, 403)

    def test_sidebar_link_lands_on_the_company_being_set_up(self):
        response = self.client.get(reverse("admin:company_company_onboarding_start"))

        self.assertRedirects(response, reverse("admin:company_company_onboarding", args=[self.company.pk]))


class OnboardingWalkthroughTest(TestCase):
    """Staff create the company; its admin goes through every step and finishes."""

    def post_step(self, key, data):
        url = reverse("admin:company_company_onboarding_step", args=[self.company.pk, key])
        response = self.client.post(url, data)
        if response.status_code == 200:
            self.fail(f"{key}: {response.context['form'].errors.as_text()}")
        self.assertRedirects(response, url)

    def test_new_company_through_finish(self):
        staff = User.objects.create_superuser("staff", "staff@scalewave.es", "x")
        self.client.force_login(staff)
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(reverse("admin:company_company_onboarding_new"), {
                "name": "NewCo", "legal_name": "NewCo SL", "tax_identifier": "B99999999",
                "constituted_country": "ES", "logo": logo(), "chart_template": pgc().pk,
                "modules": [CompanyModule.INVENTORY, CompanyModule.PRODUCTION],
                "admin_email": "admin@newco.es", "admin_first_name": "Ana", "admin_last_name": "López",
            })
        self.company = Company.objects.get(name="NewCo")
        self.assertRedirects(response, reverse("admin:company_company_onboarding", args=[self.company.pk]))

        self.client.force_login(User.objects.get(email="admin@newco.es"))
        self.assertEqual(
            self.client.get(reverse("admin:company_company_onboarding", args=[self.company.pk])).status_code, 200,
        )
        self.post_step("details", {
            "legal_name": "NewCo SL", "tax_identifier": "B99999999", "address": "Calle Mayor 1, Madrid",
            "email": "hola@newco.es", "phone": "910000000", "sepa_creditor_id": "",
            "invoice_number_format": "F-{year}-{number}", "proforma_number_format": "PF-{year}-{number}",
            "debit_note_number_format": "SUP-{year}-{number}",
        })
        self.post_step("accounting", {"ledger_start_date": "2026-11-01"})
        self.post_step("taxes", {"tax_type": get_iva21().pk, "selected_percentage": "21"})
        self.post_step("bank", {"bank_name": "Banco Uno", "iban": "ES0012345678", "is_default": "on"})
        self.post_step("inventory", {"code": "MAIN", "name": "Main warehouse"})
        self.post_step("production", {
            "production_stages": "Corte\nMontaje", "weekdays": ["0", "1", "2", "3", "4"],
            "start_time": "08:00", "end_time": "16:00",
        })

        self.company.refresh_from_db()
        self.assertEqual(self.company.production_stages, ["Corte", "Montaje"])
        self.assertEqual(self.company.schedule_slots.count(), 5)
        self.assertTrue(CompanyReadiness(self.company).is_ready())

        response = self.client.post(reverse("admin:company_company_onboarding_finish", args=[self.company.pk]))

        self.assertRedirects(response, reverse("admin:index"), fetch_redirect_response=False)
        self.company.refresh_from_db()
        self.assertIsNotNone(self.company.onboarding_completed_at)

    def test_every_step_page_renders(self):
        self.company = make_shell(self, modules=CompanyModule.values)
        self.client.force_login(User.objects.get(email="admin@newco.es"))

        for step in CompanyReadiness(self.company).steps:
            with self.subTest(step=step.key):
                response = self.client.get(
                    reverse("admin:company_company_onboarding_step", args=[self.company.pk, step.key])
                )
                self.assertEqual(response.status_code, 200)

    def test_finish_is_refused_while_a_required_step_is_missing(self):
        self.company = make_shell(self)
        self.client.force_login(User.objects.get(email="admin@newco.es"))

        self.client.post(reverse("admin:company_company_onboarding_finish", args=[self.company.pk]))

        self.company.refresh_from_db()
        self.assertIsNone(self.company.onboarding_completed_at)

    def test_invalid_form_is_shown_again_without_saving(self):
        self.company = make_shell(self)
        self.client.force_login(User.objects.get(email="admin@newco.es"))
        url = reverse("admin:company_company_onboarding_step", args=[self.company.pk, "details"])

        response = self.client.post(url, {
            "legal_name": "NewCo SL", "invoice_number_format": "X-{number}", "debit_note_number_format": "X-{number}",
        })

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["form"].errors)


def workbook(headers, *rows):
    wb = openpyxl.Workbook()
    wb.active.append(headers)
    for row in rows:
        wb.active.append(row)
    buffer = BytesIO()
    wb.save(buffer)
    buffer.seek(0)
    return SimpleUploadedFile("invoices.xlsx", buffer.read())


class OpenInvoicesImportTest(TestCase):
    """The import a customer runs goes into their company, whatever the file says."""

    def setUp(self):
        get_iva21()
        self.company = make_shell(self)
        self.other = make_company(name="Other")
        self.client.force_login(User.objects.get(email="admin@newco.es"))
        self.url = reverse("admin:company_company_onboarding_step", args=[self.company.pk, "open-invoices"])

    def test_sales_invoices_go_to_the_company_being_set_up(self):
        file = workbook(
            ["invoice_number", "date_issued", "company", "client_cif", "client_name", "base_amount",
             "iva_percentage", "iva_amount", "total"],
            ["V-1", "01/10/2026", "Other", "B22222222", "Acme", 100, 21, 21, 121],
        )

        self.client.post(self.url, {"kind": "sales", "file": file})

        invoice = SalesInvoice.objects.get(invoice_number="V-1")
        self.assertEqual(invoice.company, self.company)
        self.assertEqual(invoice.client.company, self.company)

    def test_purchase_invoices_go_to_the_company_being_set_up(self):
        file = workbook(
            ["invoice_number", "date_issued", "provider_cif", "provider_name", "base_amount",
             "iva_percentage", "iva_amount", "total", "company"],
            ["F-1", "01/10/2026", "B11111111", "Perfiles SA", 100, 21, 21, 121, "Other"],
        )

        self.client.post(self.url, {"kind": "purchases", "file": file})

        invoice = Invoice.objects.get(invoice_number="F-1")
        self.assertEqual(invoice.company, self.company)
        self.assertEqual(invoice.provider.company, self.company)


class ImportPromptsTest(TestCase):
    """A file with exactly the headers a prompt asks for imports as the prompt says."""

    def setUp(self):
        self.enterContext(translation.override("en"))
        get_iva21()
        self.company = make_shell(self, modules=[CompanyModule.INVENTORY])
        Warehouse.objects.create(company=self.company, code="MAIN", name="Main")

    def prompt(self, step_key, title):
        step = CompanyReadiness(self.company).step(step_key)
        return next(prompt for prompt in step.import_prompts() if prompt.title == title)

    def rows(self, prompt, *samples):
        """The prompt's headers, and each sample given by column position (None = cell left empty)."""
        headers = [column.header for column in prompt.columns]
        for sample in samples:
            self.assertEqual(len(sample), len(headers))
        return headers, samples

    def test_the_step_page_offers_the_prompts(self):
        self.client.force_login(User.objects.get(email="admin@newco.es"))

        response = self.client.get(
            reverse("admin:company_company_onboarding_step", args=[self.company.pk, "open-invoices"])
        )

        self.assertContains(response, "client_cif")
        self.assertContains(response, "provider_cif")

    def test_sales_invoice_prompt(self):
        from invoices.services.import_sales_invoice_service import import_sales_invoices_from_excel

        prompt = self.prompt("open-invoices", "Sales invoices: what clients owe you")
        headers, rows = self.rows(
            prompt,
            ["V-1", "01/10/2026", "31/10/2026", "B22222222", "Acme", "Ventanas", 100, 21, 21, None, 121],
            ["V-1", "01/10/2026", "31/10/2026", "B22222222", "Acme", "Ventanas", 50, 10, 5, None, 55],
        )

        result = import_sales_invoices_from_excel(workbook(headers, *rows), company=self.company)

        self.assertEqual((result.created, result.errors), (1, 0), result.rows)
        self.assertEqual(SalesInvoice.objects.get(invoice_number="V-1").company, self.company)

    def test_purchase_invoice_prompt(self):
        from invoices.services.invoice.import_invoice_service import import_invoices_from_excel

        prompt = self.prompt("open-invoices", "Purchase invoices: what you owe providers")
        headers, rows = self.rows(
            prompt, ["F-1", "01/10/2026", "B11111111", "Perfiles SA", "Perfiles", 100, 21, 21, 15, 106],
        )

        result = import_invoices_from_excel(workbook(headers, *rows), company=self.company)

        self.assertEqual((result.created, result.errors), (1, 0), result.rows)
        self.assertEqual(Invoice.objects.get(invoice_number="F-1").provider.company, self.company)

    def test_client_prompt(self):
        from sales.models import Client
        from sales.resources import ClientResource

        prompt = self.prompt("partners", "Clients")
        headers, rows = self.rows(
            prompt, ["Acme", "B22222222", "Calle Mayor 1", "28001", "Madrid", "Madrid", "910000000", "a@acme.es"],
        )

        result = ClientResource(default_company=self.company).import_data(
            tablib.Dataset(*rows, headers=headers), dry_run=False, raise_errors=True,
        )

        self.assertFalse(result.has_validation_errors())
        client = Client.objects.get(company=self.company)
        self.assertEqual((client.name, client.tax_identifier), ("Acme", "B22222222"))
        self.assertEqual(client.address, "Calle Mayor 1, 28001 Madrid (Madrid)")

    def test_item_prompt_in_the_users_language(self):
        from inventory.models import InventoryItem
        from inventory.resources import InventoryItemResource

        for language in ("en", "es"):
            with self.subTest(language=language), translation.override(language):
                prompt = self.prompt("inventory", translation.gettext("Items"))
                self.assertIn("MAIN", prompt.text)
                code = f"P-{language}"
                headers, rows = self.rows(
                    prompt, [None, self.company.name, code, "Perfil", "", "", "STOCKABLE", "MAIN", 12.5, 8, 5],
                )

                result = InventoryItemResource().import_data(
                    tablib.Dataset(*rows, headers=headers), dry_run=False, raise_errors=True,
                )

                self.assertFalse(result.has_validation_errors(), result.invalid_rows)
                item = InventoryItem.objects.get(code=code)
                self.assertEqual((item.company, item.default_warehouse.code), (self.company, "MAIN"))
                self.assertEqual(item.selling_price.amount, 12.5)
