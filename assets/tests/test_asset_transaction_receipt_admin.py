"""
assets/tests/test_asset_transaction_receipt_admin.py

Covers AssetTransactionReceiptAdmin.search_fields -- specifically that it can
find a verteo receipt by its linked AssetTransaction.reference_number (the
sale/customer delivery note the driver enters), not just by nro_albaran (the
vertedero's own albarán).
"""
from django.contrib.admin.sites import AdminSite
from django.contrib.auth import get_user_model
from django.test import RequestFactory, TestCase
from django.utils import timezone

from api.fleet.tests.helpers import make_asset, make_asset_type, make_company
from assets.admin.asset_transaction import AssetTransactionReceiptAdmin
from assets.models import AssetTransaction, AssetTransactionReceipt, TransactionTypeChoices

User = get_user_model()


class AssetTransactionReceiptAdminSearchTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_superuser(username="admin", password="password", email="admin@test.com")
        cls.company = make_company()
        cls.asset_type = make_asset_type(cls.company)
        cls.asset = make_asset(cls.company, cls.asset_type)
        cls.match_tx = AssetTransaction.objects.create(
            company=cls.company, asset=cls.asset,
            transaction_type=TransactionTypeChoices.RECEIPT, date=timezone.now(),
            reference_number="SALE-00123",
        )
        cls.match = AssetTransactionReceipt.objects.create(transaction=cls.match_tx, nro_albaran="ALB-001")
        cls.non_match_tx = AssetTransaction.objects.create(
            company=cls.company, asset=cls.asset,
            transaction_type=TransactionTypeChoices.RECEIPT, date=timezone.now(),
            reference_number="UNRELATED-999",
        )
        cls.non_match = AssetTransactionReceipt.objects.create(transaction=cls.non_match_tx, nro_albaran="ALB-002")

    def setUp(self):
        self.factory = RequestFactory()
        self.admin = AssetTransactionReceiptAdmin(AssetTransactionReceipt, AdminSite())

    def _search(self, term):
        request = self.factory.get("/admin/assets/assettransactionreceipt/")
        request.user = self.user
        queryset, _ = self.admin.get_search_results(request, AssetTransactionReceipt.objects.all(), term)
        return queryset

    def test_finds_a_receipt_by_its_transactions_sale_reference_number(self):
        results = self._search("SALE-00123")
        self.assertIn(self.match, results)
        self.assertNotIn(self.non_match, results)

    def test_still_finds_a_receipt_by_its_own_nro_albaran(self):
        results = self._search("ALB-002")
        self.assertIn(self.non_match, results)
        self.assertNotIn(self.match, results)

    def test_list_display_column_reads_the_transactions_reference_number(self):
        self.assertEqual(self.admin.transaction_reference_number(self.match), "SALE-00123")
