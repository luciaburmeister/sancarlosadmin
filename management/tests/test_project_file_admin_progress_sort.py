"""The Progress column on the ProjectFile changelist sorts by the same
precedence ProjectFile.operational_status uses."""
from django.contrib.admin.sites import site
from django.contrib.auth import get_user_model
from django.test import RequestFactory, TestCase

from company.models import Company
from management.admin.project_file import ProjectFileAdmin
from management.models import (
    ProjectFile,
    ProjectFileDeliveryStatus,
    ProjectFileProductionStatus,
)
from sales.models import Client


class ProjectFileAdminProgressSortTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Test Co")
        self.client_obj = Client.objects.create(name="Acme", company=self.company)
        self.user = get_user_model().objects.create_superuser(
            username="admin", email="admin@example.com", password="x",
        )
        self.admin = ProjectFileAdmin(ProjectFile, site)

    def _pf(self, name, delivery, production):
        pf = ProjectFile.objects.create(
            name=name, company=self.company, client=self.client_obj,
        )
        # Bypass save()/signals so the derived statuses stay as set here.
        ProjectFile.objects.filter(pk=pf.pk).update(
            delivery_status=delivery, production_status=production,
        )
        return pf

    def test_progress_ordering_follows_operational_status_precedence(self):
        self._pf("nothing", ProjectFileDeliveryStatus.NOT_APPLICABLE, ProjectFileProductionStatus.NO_PRODUCTION)
        self._pf("production", ProjectFileDeliveryStatus.NOT_DELIVERED, ProjectFileProductionStatus.IN_PROGRESS)
        self._pf("ready", ProjectFileDeliveryStatus.NOT_DELIVERED, ProjectFileProductionStatus.DONE)
        # Delivery wins even while production is still pending.
        self._pf("partial", ProjectFileDeliveryStatus.PARTIALLY_DELIVERED, ProjectFileProductionStatus.PENDING)
        self._pf("delivered", ProjectFileDeliveryStatus.DELIVERED, ProjectFileProductionStatus.DONE)

        request = RequestFactory().get("/")
        request.user = self.user
        ordering = self.admin.get_list_display(request)
        self.assertIn("show_operational_status", ordering)

        qs = self.admin.get_queryset(request).order_by("_operational_rank", "name")
        names = list(qs.values_list("name", flat=True))
        self.assertEqual(names, ["nothing", "production", "ready", "partial", "delivered"])

        # The display label for each row matches the rank order.
        labels = [pf.operational_status[0] for pf in qs]
        self.assertEqual(
            labels,
            ["nothing_pending", "in_production", "ready_to_deliver", "partially_delivered", "delivered"],
        )

    def test_progress_column_declares_ordering(self):
        self.assertEqual(
            self.admin.show_operational_status.admin_order_field, "_operational_rank",
        )
