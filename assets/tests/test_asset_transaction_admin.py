"""
assets/tests/test_asset_transaction_admin.py

Covers AssetTransactionAdmin.search_fields -- specifically that drivers'
reference_number is actually searchable now that it's a real model field
(it couldn't be, reliably, while it lived inside the `data` JSONField).
"""
from django.contrib.admin.sites import AdminSite
from django.contrib.auth import get_user_model
from django.test import RequestFactory, TestCase
from django.utils import timezone

from api.fleet.tests.helpers import make_asset, make_asset_type, make_company
from assets.admin.asset_transaction import AssetTransactionAdmin
from assets.models import AssetTransaction, TransactionTypeChoices

User = get_user_model()


class AssetTransactionAdminSearchTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.site = AdminSite()
        self.admin = AssetTransactionAdmin(AssetTransaction, self.site)
        self.user = User.objects.create_superuser(username="admin", password="password", email="admin@test.com")
        self.company = make_company()
        self.asset_type = make_asset_type(self.company)
        self.asset = make_asset(self.company, self.asset_type)
        self.match = AssetTransaction.objects.create(
            company=self.company, asset=self.asset,
            transaction_type=TransactionTypeChoices.MOVEMENT, date=timezone.now(),
            reference_number="PO-2026-00123",
        )
        self.non_match = AssetTransaction.objects.create(
            company=self.company, asset=self.asset,
            transaction_type=TransactionTypeChoices.MOVEMENT, date=timezone.now(),
            reference_number="UNRELATED-999",
        )

    def _search(self, term):
        request = self.factory.get("/admin/assets/assettransaction/")
        request.user = self.user
        queryset, _ = self.admin.get_search_results(request, AssetTransaction.objects.all(), term)
        return queryset

    def test_finds_a_transaction_by_its_reference_number(self):
        results = self._search("PO-2026-00123")
        self.assertIn(self.match, results)
        self.assertNotIn(self.non_match, results)

    def test_is_a_partial_case_insensitive_match(self):
        """Admin search_fields defaults to icontains -- drivers won't type
        the exact reference back, so partial/loose matching matters."""
        results = self._search("po-2026")
        self.assertIn(self.match, results)
