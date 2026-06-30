"""Form-level tests for ResourcePlanningBulkForm."""
import datetime

from django.test import TestCase

from company.models import Company, Employee
from management.forms import ResourcePlanningBulkForm
from management.models import ProjectFile, ResourcePlanningStatus, ResourcePlanningType
from sales.models import Client


class TestResourcePlanningBulkForm(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name="Form Co")
        cls.client = Client.objects.create(company=cls.company, name="Client")
        cls.project_file = ProjectFile.objects.create(
            company=cls.company,
            reference="EXP-00001",
            name="Project",
            status="open",
            client=cls.client,
        )
        cls.planning_type = ResourcePlanningType.objects.create(
            company=cls.company, name="Install", color="#000000",
        )
        cls.employee = Employee.objects.create(
            company=cls.company,
            first_name="Ana",
            last_name="García",
            hired_at=datetime.date(2020, 1, 1),
            is_active=True,
        )
        cls.user_companies = Company.objects.filter(pk=cls.company.pk)

    def _base_data(self, **overrides):
        data = {
            "company": str(self.company.pk),
            "project_file": str(self.project_file.pk),
            "planning_type": str(self.planning_type.pk),
            "status": ResourcePlanningStatus.PLANNED,
            "resources": f"employee:{self.employee.pk}",
            "date_from": "07/07/2026",
            "date_to": "09/07/2026",
            "daily_start": "08:00",
            "daily_end": "17:00",
        }
        data.update(overrides)
        return data

    def test_multi_day_spanish_dates_valid(self):
        form = ResourcePlanningBulkForm(data=self._base_data(), user_companies=self.user_companies)
        self.assertTrue(form.is_valid(), form.errors)

    def test_same_day_invalid_times(self):
        form = ResourcePlanningBulkForm(
            data=self._base_data(
                date_from="07/07/2026",
                date_to="07/07/2026",
                daily_start="17:00",
                daily_end="08:00",
            ),
            user_companies=self.user_companies,
        )
        self.assertFalse(form.is_valid())
        self.assertIn("daily_end", form.errors)

    def test_multi_day_inverted_clock_times_valid_when_span_ok(self):
        form = ResourcePlanningBulkForm(
            data=self._base_data(daily_start="17:00", daily_end="06:00"),
            user_companies=self.user_companies,
        )
        self.assertTrue(form.is_valid(), form.errors)

    def test_default_initial_times(self):
        form = ResourcePlanningBulkForm(user_companies=self.user_companies)
        self.assertEqual(form.fields["daily_start"].initial, datetime.time(8, 0))
        self.assertEqual(form.fields["daily_end"].initial, datetime.time(17, 0))
