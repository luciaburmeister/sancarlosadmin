"""Work-entry expression context for AssetTransactions: receipt and movement zones."""
from django.test import TestCase
from django.utils import timezone

from assets.models import AssetTransaction, AssetTransactionReceipt, TransactionTypeChoices
from assets.services.movement_context import build_asset_transaction_context
from company.models import Company, WorkingZone
from management.models import WorkEntryType


class ReceiptZoneContextTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name="Taller Co")
        cls.obra = WorkingZone.objects.create(company=cls.company, name="Obra Sur")
        cls.almacen = WorkingZone.objects.create(company=cls.company, name="Almacén")
        cls.vertedero = WorkingZone.objects.create(company=cls.company, name="Vertedero Norte")

        cls.movement = AssetTransaction.objects.create(
            company=cls.company,
            transaction_type=TransactionTypeChoices.MOVEMENT,
            from_zone=cls.obra,
            to_zone=cls.almacen,
            date=timezone.now(),
        )
        cls.receipt_tx = AssetTransaction.objects.create(
            company=cls.company,
            transaction_type=TransactionTypeChoices.RECEIPT,
            from_zone=cls.almacen,
            to_zone=cls.vertedero,
            date=timezone.now(),
        )
        cls.receipt = AssetTransactionReceipt.objects.create(
            transaction=cls.receipt_tx,
            movement_transaction=cls.movement,
            nro_albaran="ALB-1",
            data={"peso_neto": 1200, "material_vertido": "escombro"},
        )

    def test_movement_sees_receipt_zones(self):
        ctx = build_asset_transaction_context(self.movement)
        self.assertEqual(ctx["to_zone"], str(self.almacen))
        receipt = ctx["receipt"]
        self.assertEqual(receipt["to_zone"], str(self.vertedero))
        self.assertEqual(receipt["to_zone"], "Vertedero Norte (Taller Co)")
        self.assertEqual(receipt["to_zone_name"], "Vertedero Norte")
        self.assertEqual(receipt["from_zone"], str(self.almacen))
        self.assertEqual(receipt["from_zone_name"], "Almacén")
        self.assertEqual(receipt["nro_albaran"], "ALB-1")
        self.assertEqual(receipt["peso_neto"], 1200)
        self.assertEqual(receipt["date"], self.receipt_tx.date)

    def test_receipt_sees_own_receipt_and_movement_zones(self):
        ctx = build_asset_transaction_context(self.receipt_tx)
        self.assertEqual(ctx["to_zone"], str(self.vertedero))
        self.assertEqual(ctx["receipt"]["to_zone"], str(self.vertedero))
        self.assertEqual(ctx["movement"]["to_zone"], str(self.almacen))
        self.assertEqual(ctx["movement"]["from_zone"], str(self.obra))

    def test_receipt_without_zones_yields_none(self):
        bare_tx = AssetTransaction.objects.create(
            company=self.company,
            transaction_type=TransactionTypeChoices.RECEIPT,
            date=timezone.now(),
        )
        AssetTransactionReceipt.objects.create(transaction=bare_tx, nro_albaran="ALB-2", data={})
        ctx = build_asset_transaction_context(bare_tx)
        self.assertIsNone(ctx["receipt"]["to_zone"])
        self.assertIsNone(ctx["receipt"]["to_zone_name"])
        self.assertIsNone(ctx["receipt"]["from_zone"])
        self.assertIsNone(ctx["receipt"]["from_zone_name"])

    def test_applicability_expression_can_match_receipt_zone(self):
        wtype = WorkEntryType(
            company=self.company,
            name="Verteo Norte",
            applicability_expression=f"receipt.to_zone == '{self.vertedero}'",
        )
        ctx = build_asset_transaction_context(self.movement)
        self.assertTrue(wtype.evaluate_applicability(ctx))

        wtype.applicability_expression = "receipt.to_zone == 'Otro'"
        self.assertFalse(wtype.evaluate_applicability(ctx))

        wtype.applicability_expression = "receipt.to_zone_name == 'Vertedero Norte'"
        self.assertTrue(wtype.evaluate_applicability(ctx))

    def test_escaped_material_matches_catalog_name(self):
        receipt_tx = AssetTransaction.objects.create(
            company=self.company,
            transaction_type=TransactionTypeChoices.RECEIPT,
            date=timezone.now(),
        )
        AssetTransactionReceipt.objects.create(
            transaction=receipt_tx,
            nro_albaran="ALB-3",
            data={
                "material_vertido": "Hormig\\u00f3n",
                "peso_neto": "9800",
            },
        )
        ordinal_tx = AssetTransaction.objects.create(
            company=self.company,
            transaction_type=TransactionTypeChoices.RECEIPT,
            date=timezone.now(),
        )
        AssetTransactionReceipt.objects.create(
            transaction=ordinal_tx,
            nro_albaran="ALB-4",
            data={"material_vertido": "2\\u00ba RCD"},
        )

        concrete = WorkEntryType(
            company=self.company,
            name="Hormigon",
            applicability_expression="receipt.material_vertido == 'Hormigón'",
        )
        ordinal = WorkEntryType(
            company=self.company,
            name="Segundo RCD",
            applicability_expression="receipt.material_vertido == '2º RCD'",
        )

        self.assertTrue(concrete.evaluate_applicability(build_asset_transaction_context(receipt_tx)))
        self.assertTrue(ordinal.evaluate_applicability(build_asset_transaction_context(ordinal_tx)))
