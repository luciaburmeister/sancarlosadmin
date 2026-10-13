from django.test import TestCase

from company.admin.company import CompanyAdmin, CompanyAdminForm
from company.models import Company


class CompanyClaudeAPIKeyTests(TestCase):
    def test_key_roundtrips_and_is_not_stored_in_plaintext(self):
        company = Company.objects.create(name="Key Co", tax_identifier="K1")
        company.set_claude_api_key("sk-ant-secret")
        company.save(update_fields=["claude_api_key_encrypted"])

        company.refresh_from_db()
        self.assertEqual(company.get_claude_api_key(), "sk-ant-secret")
        self.assertNotIn("sk-ant-secret", company.claude_api_key_encrypted)

    def test_key_field_is_in_application_configurations(self):
        groups = [
            options["fields"]
            for _title, options in CompanyAdmin.fieldsets
            if "claude_api_key" in options["fields"]
        ]
        self.assertEqual(len(groups), 1)
        self.assertIn("ideal_daily_work_minutes", groups[0])

    def test_admin_form_exposes_a_write_only_key_field(self):
        company = Company.objects.create(name="Key Co", tax_identifier="K3")
        company.set_claude_api_key("sk-ant-secret")
        company.save(update_fields=["claude_api_key_encrypted"])

        form = CompanyAdminForm(instance=company)
        self.assertIn("claude_api_key", form.fields)
        self.assertNotIn("claude_api_key_encrypted", form.fields)
        self.assertEqual(form["claude_api_key"].value(), None)

    def test_blank_clears_the_stored_key(self):
        company = Company.objects.create(name="Key Co", tax_identifier="K2")
        company.set_claude_api_key("sk-ant-secret")
        company.set_claude_api_key("  ")
        self.assertEqual(company.claude_api_key_encrypted, "")
        self.assertEqual(company.get_claude_api_key(), "")
