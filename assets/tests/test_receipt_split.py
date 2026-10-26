"""
assets/tests/test_receipt_split.py

Covers AssetTransactionReceipt.split_into_parts, the admin wizard that drives
it (ReceiptSplitWizardView), and its entry point on AssetTransactionAdmin --
lets plant/office staff turn one DI into several when a cuba comes back mixed
with more than one residue.
"""
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from api.fleet.tests.helpers import make_asset, make_asset_type, make_company
from assets.constants import MAX_RECEIPT_SPLIT_PARTS
from assets.models import AssetTransaction, AssetTransactionReceipt, TransactionTypeChoices

User = get_user_model()


class SplitIntoPartsModelTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = make_company()
        cls.asset_type = make_asset_type(cls.company)
        cls.asset = make_asset(cls.company, cls.asset_type)
        cls.transaction = AssetTransaction.objects.create(
            company=cls.company, asset=cls.asset,
            transaction_type=TransactionTypeChoices.RECEIPT, date=timezone.now(),
        )

    def _make_receipt(self):
        return AssetTransactionReceipt.objects.create(
            transaction=self.transaction, nro_albaran="01083485",
            data={"material_vertido": "Madera"},
        )

    def test_split_creates_siblings_sharing_the_transaction(self):
        receipt = self._make_receipt()

        siblings = receipt.split_into_parts(3)

        self.assertEqual(len(siblings), 2)
        receipt.refresh_from_db()
        self.assertEqual((receipt.part_number, receipt.total_parts), (1, 3))
        self.assertEqual(receipt.nro_albaran, "01083485")  # original untouched
        for n, sibling in enumerate(siblings, start=2):
            self.assertEqual(sibling.transaction_id, self.transaction.pk)
            self.assertEqual((sibling.part_number, sibling.total_parts), (n, 3))
            # Same albaran/physical load -- inherited, not left blank.
            self.assertEqual(sibling.nro_albaran, "01083485")
            # material_vertido is the one thing that genuinely differs per
            # part, so it's NOT inherited -- stays blank for staff to fill in.
            self.assertNotIn("material_vertido", sibling.data)
        self.assertEqual(
            AssetTransactionReceipt.objects.filter(transaction=self.transaction).count(), 3,
        )

    def test_split_does_not_inherit_peso_neto_from_the_receipt_being_split(self):
        """peso_neto genuinely differs per part (splitting one load by
        residue type rarely weighs the same in each part), so -- unlike
        nro_albaran -- it's left for staff to fill in per part, not copied."""
        receipt = self._make_receipt()
        receipt.data["peso_neto"] = "500"
        receipt.save(update_fields=["data"])

        siblings = receipt.split_into_parts(3)

        for sibling in siblings:
            self.assertNotIn("peso_neto", sibling.data)

    def test_adding_more_parts_later_still_does_not_inherit_peso_neto(self):
        receipt = self._make_receipt()
        receipt.data["peso_neto"] = "500"
        receipt.save(update_fields=["data"])
        receipt.split_into_parts(2)

        new_siblings = receipt.split_into_parts(4)

        for sibling in new_siblings:
            self.assertEqual(sibling.nro_albaran, "01083485")
            self.assertNotIn("peso_neto", sibling.data)

    def test_can_add_more_parts_to_an_already_split_receipt(self):
        receipt = self._make_receipt()
        receipt.split_into_parts(2)

        new_siblings = receipt.split_into_parts(4)

        self.assertEqual(len(new_siblings), 2)
        self.assertEqual([s.part_number for s in new_siblings], [3, 4])
        self.assertEqual(
            list(
                AssetTransactionReceipt.objects.filter(transaction=self.transaction)
                .order_by("part_number").values_list("total_parts", flat=True)
            ),
            [4, 4, 4, 4],
        )

    def test_can_add_more_parts_from_a_generated_sibling(self):
        receipt = self._make_receipt()
        sibling = receipt.split_into_parts(2)[0]

        sibling.split_into_parts(3)

        self.assertEqual(
            AssetTransactionReceipt.objects.filter(transaction=self.transaction).count(), 3,
        )
        self.assertTrue(
            AssetTransactionReceipt.objects.filter(transaction=self.transaction).values_list(
                "total_parts", flat=True
            ).distinct().count() == 1
        )

    def test_total_parts_out_of_bounds_is_rejected(self):
        receipt = self._make_receipt()

        with self.assertRaises(ValidationError):
            receipt.split_into_parts(1)  # not greater than the current 1
        with self.assertRaises(ValidationError):
            receipt.split_into_parts(MAX_RECEIPT_SPLIT_PARTS + 1)


class ReceiptPartNumberValidationTests(TestCase):
    """clean() only guards an actual split family (total_parts > 1) -- a
    legacy/unsplit receipt (the default for every pre-existing row) must
    stay editable even if it happens to share a transaction with another
    unsplit receipt, which the app already tolerated before this existed."""

    @classmethod
    def setUpTestData(cls):
        cls.company = make_company()
        cls.asset_type = make_asset_type(cls.company)
        cls.asset = make_asset(cls.company, cls.asset_type)
        cls.transaction = AssetTransaction.objects.create(
            company=cls.company, asset=cls.asset,
            transaction_type=TransactionTypeChoices.RECEIPT, date=timezone.now(),
        )

    def test_legacy_unsplit_receipts_sharing_a_transaction_stay_editable(self):
        first = AssetTransactionReceipt.objects.create(transaction=self.transaction, nro_albaran="A")
        AssetTransactionReceipt.objects.create(transaction=self.transaction, nro_albaran="B")

        first.nro_albaran = "A-edited"
        first.full_clean()  # must not raise

    def test_duplicate_part_number_within_a_split_family_is_rejected(self):
        receipt = AssetTransactionReceipt.objects.create(transaction=self.transaction, nro_albaran="A")
        siblings = receipt.split_into_parts(3)

        siblings[0].part_number = siblings[1].part_number

        with self.assertRaises(ValidationError):
            siblings[0].full_clean()

    def test_part_number_greater_than_total_parts_is_rejected(self):
        receipt = AssetTransactionReceipt.objects.create(transaction=self.transaction, nro_albaran="A")
        siblings = receipt.split_into_parts(2)

        siblings[0].part_number = 99

        with self.assertRaises(ValidationError):
            siblings[0].full_clean()


class ReceiptSplitWizardViewTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = make_company()
        cls.asset_type = make_asset_type(cls.company)
        cls.asset = make_asset(cls.company, cls.asset_type)
        cls.transaction = AssetTransaction.objects.create(
            company=cls.company, asset=cls.asset,
            transaction_type=TransactionTypeChoices.RECEIPT, date=timezone.now(),
        )

    def setUp(self):
        self.receipt = AssetTransactionReceipt.objects.create(
            transaction=self.transaction, nro_albaran="01083485",
        )
        self.user = User.objects.create_superuser(username="admin", password="password", email="admin@test.com")
        self.client.force_login(self.user)
        self.url = reverse("admin:assets_assettransactionreceipt_split", args=[self.receipt.pk])

    def test_posting_a_count_splits_the_receipt(self):
        response = self.client.post(self.url, {"total_parts": "3"})

        self.assertRedirects(
            response, reverse("admin:assets_assettransaction_change", args=[self.transaction.pk]),
        )
        self.assertEqual(AssetTransactionReceipt.objects.filter(transaction=self.transaction).count(), 3)

    def test_posting_a_higher_count_adds_more_parts_to_an_already_split_receipt(self):
        self.receipt.split_into_parts(2)

        response = self.client.post(self.url, {"total_parts": "4"})

        self.assertRedirects(
            response, reverse("admin:assets_assettransaction_change", args=[self.transaction.pk]),
        )
        self.assertEqual(AssetTransactionReceipt.objects.filter(transaction=self.transaction).count(), 4)

    def test_posting_a_count_not_greater_than_current_is_rejected(self):
        self.receipt.split_into_parts(3)

        response = self.client.post(self.url, {"total_parts": "2"})

        self.assertEqual(response.status_code, 200)  # form re-rendered with an error
        self.assertEqual(AssetTransactionReceipt.objects.filter(transaction=self.transaction).count(), 3)


class TransactionSplitActionDisplayTests(TestCase):
    """The entry point lives on AssetTransactionAdmin -- that's where plant/
    office staff actually look up a verteo, not the receipt's own page."""

    @classmethod
    def setUpTestData(cls):
        cls.company = make_company()
        cls.asset_type = make_asset_type(cls.company)
        cls.asset = make_asset(cls.company, cls.asset_type)

    def setUp(self):
        self.user = User.objects.create_superuser(username="admin", password="password", email="admin@test.com")
        self.client.force_login(self.user)

    def _change_page(self, transaction):
        url = reverse("admin:assets_assettransaction_change", args=[transaction.pk])
        return self.client.get(url)

    def test_shows_split_link_for_an_unsplit_receipt(self):
        transaction = AssetTransaction.objects.create(
            company=self.company, asset=self.asset,
            transaction_type=TransactionTypeChoices.RECEIPT, date=timezone.now(),
        )
        receipt = AssetTransactionReceipt.objects.create(transaction=transaction, nro_albaran="01083485")

        response = self._change_page(transaction)

        split_url = reverse("admin:assets_assettransactionreceipt_split", args=[receipt.pk])
        self.assertContains(response, split_url)

    def test_shows_add_more_parts_link_once_already_split(self):
        transaction = AssetTransaction.objects.create(
            company=self.company, asset=self.asset,
            transaction_type=TransactionTypeChoices.RECEIPT, date=timezone.now(),
        )
        receipt = AssetTransactionReceipt.objects.create(transaction=transaction, nro_albaran="01083485")
        receipt.split_into_parts(2)

        response = self._change_page(transaction)

        split_url = reverse("admin:assets_assettransactionreceipt_split", args=[receipt.pk])
        self.assertContains(response, split_url)
        self.assertContains(response, "rtes de DI")  # "Añadir más partes de DI..." (es is the default UI language)

    def test_no_link_for_a_transaction_without_a_receipt(self):
        transaction = AssetTransaction.objects.create(
            company=self.company, asset=self.asset,
            transaction_type=TransactionTypeChoices.MOVEMENT, date=timezone.now(),
        )

        response = self._change_page(transaction)

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "/split/")
