"""The expediente action queues the same work-entry task as the transaction list."""
from unittest.mock import patch

from django.contrib.admin.sites import site
from django.contrib.messages.storage.fallback import FallbackStorage
from django.test import RequestFactory, TestCase
from django.urls import reverse
from django.utils import timezone

from assets.models import AssetTransaction, TransactionTypeChoices
from company.models import Company
from management.admin.project_file import ProjectFileAdmin
from management.models import ProjectFile
from sales.models import Client


class ProjectFileWorkEntriesActionTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name="Taller Co")
        cls.client_obj = Client.objects.create(name="Acme", company=cls.company)
        cls.project_file = ProjectFile.objects.create(
            company=cls.company, name="PF1", client=cls.client_obj,
        )
        cls.other = ProjectFile.objects.create(
            company=cls.company, name="PF2", client=cls.client_obj,
        )
        cls.own = AssetTransaction.objects.create(
            company=cls.company, project_file=cls.project_file,
            transaction_type=TransactionTypeChoices.MOVEMENT, date=timezone.now(),
        )
        AssetTransaction.objects.create(
            company=cls.company, project_file=cls.other,
            transaction_type=TransactionTypeChoices.MOVEMENT, date=timezone.now(),
        )

    def setUp(self):
        self.admin = ProjectFileAdmin(ProjectFile, site)
        self.request = RequestFactory().get("/")
        self.request.session = {}
        self.request._messages = FallbackStorage(self.request)

    def test_queues_only_this_project_files_transactions(self):
        with patch("core.tasks.generate_work_entries_task.delay") as delay:
            response = self.admin.generate_work_entries_row_action(
                self.request, self.project_file.pk,
            )
        delay.assert_called_once_with([self.own.pk])
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            response.url,
            reverse("admin:management_projectfile_change", args=[self.project_file.pk]),
        )

    def test_empty_project_file_does_not_queue(self):
        empty = ProjectFile.objects.create(
            company=self.company, name="Empty", client=self.client_obj,
        )
        with patch("core.tasks.generate_work_entries_task.delay") as delay:
            self.admin.generate_work_entries_row_action(self.request, empty.pk)
        delay.assert_not_called()
