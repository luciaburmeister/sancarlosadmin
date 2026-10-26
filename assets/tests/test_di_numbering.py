"""
assets/tests/test_di_numbering.py

Covers the DI (Documento de Identificación) sequential number: a full-cuba
MOVEMENT pickup, a porte PORTE_LOAD, and every AssetTransactionReceipt. A DI
is tied to the plan -- the movement that picks up a full cuba and the verteo
that empties it are the same document, so the first receipt filed against
that movement must inherit its number, not get a new one; only a split's
extra parts are genuinely separate DIs.
"""
from django.core.exceptions import ValidationError
from django.test import RequestFactory, TestCase
from django.utils import timezone

from api.fleet.tests.helpers import make_asset, make_asset_type, make_company
from assets.admin.asset_transaction import HasDiNumberFilter
from assets.models import (
    AssetTransaction,
    AssetTransactionPause,
    AssetTransactionReceipt,
    TransactionTypeChoices,
)


class DiNumberAssignmentTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = make_company()
        cls.asset_type = make_asset_type(cls.company)
        cls.asset = make_asset(cls.company, cls.asset_type)

    def _full_cuba_movement(self):
        return AssetTransaction.objects.create(
            company=self.company, asset=self.asset,
            transaction_type=TransactionTypeChoices.MOVEMENT, date=timezone.now(),
            data={"cubaEstado": "Cuba llena"},
        )

    def test_full_cuba_movement_gets_a_di_number(self):
        movement = self._full_cuba_movement()

        self.assertIsNotNone(movement.di_number)

    def test_non_full_movement_gets_no_di_number(self):
        movement = AssetTransaction.objects.create(
            company=self.company, asset=self.asset,
            transaction_type=TransactionTypeChoices.MOVEMENT, date=timezone.now(),
            data={"cubaEstado": "Cuba vacía"},
        )

        self.assertIsNone(movement.di_number)

    def test_porte_load_gets_a_di_number(self):
        tx = AssetTransaction.objects.create(
            company=self.company, asset=self.asset,
            transaction_type=TransactionTypeChoices.PORTE_LOAD, date=timezone.now(),
        )

        self.assertIsNotNone(tx.di_number)

    def test_porte_movement_gets_no_di_number(self):
        """The driver flow is pickup empty cuba (PORTE_MOVEMENT) -> load
        (PORTE_LOAD) -> deliver -- the pickup has no material in custody
        yet, so it never carries a DI."""
        tx = AssetTransaction.objects.create(
            company=self.company, asset=self.asset,
            transaction_type=TransactionTypeChoices.PORTE_MOVEMENT, date=timezone.now(),
        )

        self.assertIsNone(tx.di_number)

    def test_receipt_inherits_its_movements_di_number(self):
        movement = self._full_cuba_movement()
        receipt_tx = AssetTransaction.objects.create(
            company=self.company, asset=self.asset,
            transaction_type=TransactionTypeChoices.RECEIPT, date=timezone.now(),
        )

        receipt = AssetTransactionReceipt.objects.create(
            transaction=receipt_tx, nro_albaran="01083485", movement_transaction=movement,
        )

        self.assertEqual(receipt.di_number, movement.di_number)

    def test_receipt_without_a_movement_gets_its_own_number(self):
        receipt_tx = AssetTransaction.objects.create(
            company=self.company, asset=self.asset,
            transaction_type=TransactionTypeChoices.RECEIPT, date=timezone.now(),
        )

        receipt = AssetTransactionReceipt.objects.create(transaction=receipt_tx, nro_albaran="01083485")

        self.assertIsNotNone(receipt.di_number)

    def test_split_parts_get_their_own_new_numbers_not_the_movements(self):
        movement = self._full_cuba_movement()
        receipt_tx = AssetTransaction.objects.create(
            company=self.company, asset=self.asset,
            transaction_type=TransactionTypeChoices.RECEIPT, date=timezone.now(),
        )
        receipt = AssetTransactionReceipt.objects.create(
            transaction=receipt_tx, nro_albaran="01083485", movement_transaction=movement,
        )
        self.assertEqual(receipt.di_number, movement.di_number)

        siblings = receipt.split_into_parts(3)

        sibling_numbers = {s.di_number for s in siblings}
        self.assertEqual(len(sibling_numbers), 2)
        self.assertNotIn(movement.di_number, sibling_numbers)
        self.assertNotIn(receipt.di_number, sibling_numbers)


class HasDiNumberFilterTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = make_company()
        cls.asset_type = make_asset_type(cls.company)
        cls.asset = make_asset(cls.company, cls.asset_type)
        cls.with_di = AssetTransaction.objects.create(
            company=cls.company, asset=cls.asset,
            transaction_type=TransactionTypeChoices.PORTE_LOAD, date=timezone.now(),
        )
        cls.without_di = AssetTransaction.objects.create(
            company=cls.company, asset=cls.asset,
            transaction_type=TransactionTypeChoices.PORTE_DELIVERY, date=timezone.now(),
        )

    def _filtered(self, value):
        request = RequestFactory().get("/")
        filt = HasDiNumberFilter(request, {"has_di": [value]}, AssetTransaction, None)
        return set(filt.queryset(request, AssetTransaction.objects.filter(company=self.company)).values_list("pk", flat=True))

    def test_yes_shows_only_numbered_transactions(self):
        self.assertEqual(self._filtered("yes"), {self.with_di.pk})

    def test_no_shows_only_unnumbered_transactions(self):
        self.assertEqual(self._filtered("no"), {self.without_di.pk})

    def test_receipt_with_a_di_counts_as_yes(self):
        """A RECEIPT transaction's own `di_number` column is always empty --
        its DI lives on the linked AssetTransactionReceipt row instead. The
        filter must still find it under "yes", not silently drop it."""
        receipt_tx = AssetTransaction.objects.create(
            company=self.company, asset=self.asset,
            transaction_type=TransactionTypeChoices.RECEIPT, date=timezone.now(),
        )
        AssetTransactionReceipt.objects.create(transaction=receipt_tx, nro_albaran="01083485")
        self.assertIsNone(receipt_tx.di_number)  # the column itself stays empty

        self.assertIn(receipt_tx.pk, self._filtered("yes"))
        self.assertNotIn(receipt_tx.pk, self._filtered("no"))


class DiNumberHandEditUniquenessTests(TestCase):
    """di_number is hand-editable by a superuser (see get_readonly_fields) --
    nothing else guards it against colliding with a number that already
    exists, so clean() has to."""

    @classmethod
    def setUpTestData(cls):
        cls.company = make_company()
        cls.asset_type = make_asset_type(cls.company)
        cls.asset = make_asset(cls.company, cls.asset_type)
        cls.other_company = make_company(name="Other Co")
        cls.other_asset_type = make_asset_type(cls.other_company)
        cls.other_asset = make_asset(cls.other_company, cls.other_asset_type)

    def _full_cuba_movement(self, company=None, asset=None):
        return AssetTransaction.objects.create(
            company=company or self.company, asset=asset or self.asset,
            transaction_type=TransactionTypeChoices.MOVEMENT, date=timezone.now(),
            data={"cubaEstado": "Cuba llena"},
        )

    def test_renaming_a_movement_to_another_movements_number_is_rejected(self):
        taken = self._full_cuba_movement()
        movement = self._full_cuba_movement()

        movement.di_number = taken.di_number
        with self.assertRaises(ValidationError):
            movement.full_clean()

    def test_renaming_a_movement_to_a_receipts_number_is_rejected(self):
        receipt_tx = AssetTransaction.objects.create(
            company=self.company, asset=self.asset,
            transaction_type=TransactionTypeChoices.RECEIPT, date=timezone.now(),
        )
        receipt = AssetTransactionReceipt.objects.create(transaction=receipt_tx, nro_albaran="A")
        movement = self._full_cuba_movement()

        movement.di_number = receipt.di_number
        with self.assertRaises(ValidationError):
            movement.full_clean()

    def test_renaming_a_movement_to_a_pauses_number_is_rejected(self):
        paused_movement = self._full_cuba_movement()
        pause = AssetTransactionPause.objects.create(movement=paused_movement)
        movement = self._full_cuba_movement()

        movement.di_number = pause.di_number
        with self.assertRaises(ValidationError):
            movement.full_clean()

    def test_renaming_a_receipt_to_a_movements_number_is_rejected(self):
        taken = self._full_cuba_movement()
        receipt_tx = AssetTransaction.objects.create(
            company=self.company, asset=self.asset,
            transaction_type=TransactionTypeChoices.RECEIPT, date=timezone.now(),
        )
        receipt = AssetTransactionReceipt.objects.create(transaction=receipt_tx, nro_albaran="A")

        receipt.di_number = taken.di_number
        with self.assertRaises(ValidationError):
            receipt.full_clean()

    def test_a_movement_sharing_its_number_with_its_own_receipt_is_fine(self):
        """The overwhelmingly common real case: a movement and the first
        receipt filed against it share one di_number by design (see
        AssetTransactionReceipt._resolve_di_number) -- full_clean() on
        either side of that legitimate pair must not treat it as a
        conflict, or saving any already-resolved movement/receipt from the
        admin would always fail."""
        movement = self._full_cuba_movement()
        receipt_tx = AssetTransaction.objects.create(
            company=self.company, asset=self.asset,
            transaction_type=TransactionTypeChoices.RECEIPT, date=timezone.now(),
        )
        receipt = AssetTransactionReceipt.objects.create(
            transaction=receipt_tx, nro_albaran="A", movement_transaction=movement,
        )
        self.assertEqual(receipt.di_number, movement.di_number)  # inherited, by design

        movement.full_clean()  # must not raise
        receipt.full_clean()  # must not raise

    def test_keeping_a_movements_own_existing_number_is_fine(self):
        """Self-exclusion works -- full_clean() without actually changing
        anything must not flag a row's own number as a conflict with itself."""
        movement = self._full_cuba_movement()

        movement.full_clean()  # must not raise

    def test_same_number_in_a_different_company_is_not_a_conflict(self):
        movement = self._full_cuba_movement()
        other_movement = self._full_cuba_movement(company=self.other_company, asset=self.other_asset)

        other_movement.di_number = movement.di_number
        other_movement.full_clean()  # must not raise -- different company, different sequence
