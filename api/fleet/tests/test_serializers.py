"""
api/fleet/tests/test_serializers.py

Tests 1-8: AssetTransactionSerializer

Run just this file:
    python manage.py test api.fleet.tests.test_serializers
"""

from django.test import TestCase

from api.fleet.serializers import AssetTransactionSerializer


class AssetTransactionSerializerTests(TestCase):
    """
    Tests for AssetTransactionSerializer.
    These check that the serializer accepts the right data,
    rejects bad data, and never silently strips fields.
    """

    def _movement_payload(self, **overrides):
        """
        Returns a valid movement payload.
        Pass keyword arguments to override specific fields.
        Example: self._movement_payload(transaction_type="delivery")
        """
        payload = {
            "transaction_type": "movement",
            "latitude": 40.416775,
            "longitude": -3.703790,
            "matricula": "1234ABC",
            "tipo_camion": "Volquete",
            "implemento": "Gancho",
            "cliente": "Cliente Test",
            "obra": "Obra Test",
            "trabajo": "Recogida",
            "tipo_cuba": "6m3",
            "m3": "6",
            "vertedero": "Vertedero Norte",
        }
        payload.update(overrides)
        return payload

    def _receipt_payload(self, **overrides):
        """Returns a valid receipt (verteo) payload."""
        payload = {
            "transaction_type": "receipt",
            "nro_albaran": "ALB-001",
            "foto_albaran": "",
            "peso_neto": "500",
            "material_vertido": "RCD Limpio",
            "latitude": 40.416775,
            "longitude": -3.703790,
        }
        payload.update(overrides)
        return payload

    # Test 1
    def test_movement_fields_are_accepted(self):
        """
        All albaran fields sent in a movement payload must survive
        into validated_data — none of them should be silently stripped.
        This is the test that would have caught the original production bug.
        """
        serializer = AssetTransactionSerializer(data=self._movement_payload())
        self.assertTrue(serializer.is_valid(), serializer.errors)

        validated = serializer.validated_data
        self.assertEqual(validated["matricula"], "1234ABC")
        self.assertEqual(validated["tipo_camion"], "Volquete")
        self.assertEqual(validated["implemento"], "Gancho")
        self.assertEqual(validated["cliente"], "Cliente Test")
        self.assertEqual(validated["obra"], "Obra Test")
        self.assertEqual(validated["trabajo"], "Recogida")
        self.assertEqual(validated["tipo_cuba"], "6m3")
        self.assertEqual(validated["m3"], "6")
        self.assertEqual(validated["vertedero"], "Vertedero Norte")

    # Test 2
    def test_receipt_fields_dont_appear_in_movement(self):
        """
        Receipt-only fields must NOT appear in a movement's validated_data.
        They have no default in the serializer so when not sent they should
        be completely absent — not empty strings polluting the transaction.
        """
        serializer = AssetTransactionSerializer(data=self._movement_payload())
        self.assertTrue(serializer.is_valid(), serializer.errors)

        validated = serializer.validated_data
        self.assertNotIn("nro_albaran", validated)
        self.assertNotIn("foto_albaran", validated)
        self.assertNotIn("peso_neto", validated)
        self.assertNotIn("material_vertido", validated)

    # Test 3
    def test_receipt_fields_dont_appear_in_delivery(self):
        """Same as above but for delivery transactions."""
        serializer = AssetTransactionSerializer(
            data=self._movement_payload(transaction_type="delivery")
        )
        self.assertTrue(serializer.is_valid(), serializer.errors)

        validated = serializer.validated_data
        self.assertNotIn("nro_albaran", validated)
        self.assertNotIn("foto_albaran", validated)
        self.assertNotIn("peso_neto", validated)
        self.assertNotIn("material_vertido", validated)

    # Test 4
    def test_invalid_transaction_type_is_rejected(self):
        """
        A typo in transaction_type must be caught by the serializer
        and return a validation error — not crash the server with a 500.
        """
        serializer = AssetTransactionSerializer(
            data=self._movement_payload(transaction_type="BLABLA")
        )
        self.assertFalse(serializer.is_valid())
        self.assertIn("transaction_type", serializer.errors)

    # Test 5
    def test_missing_transaction_type_is_rejected(self):
        """transaction_type is required. Missing it must fail validation."""
        payload = self._movement_payload()
        del payload["transaction_type"]

        serializer = AssetTransactionSerializer(data=payload)
        self.assertFalse(serializer.is_valid())
        self.assertIn("transaction_type", serializer.errors)

    # Test 6
    def test_empty_payload_is_rejected(self):
        """An empty payload {} must fail gracefully — not crash."""
        serializer = AssetTransactionSerializer(data={})
        self.assertFalse(serializer.is_valid())
        self.assertIn("transaction_type", serializer.errors)

    # Test 7
    def test_gps_coordinates_default_to_zero_when_not_sent(self):
        """
        latitude and longitude are optional.
        When not sent they default to 0 instead of failing validation.
        """
        payload = self._movement_payload()
        del payload["latitude"]
        del payload["longitude"]

        serializer = AssetTransactionSerializer(data=payload)
        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertEqual(serializer.validated_data["latitude"], 0)
        self.assertEqual(serializer.validated_data["longitude"], 0)

    # Test 8
    def test_zone_id_is_optional_and_defaults_to_none(self):
        """
        zone_id is only sent when the driver picks from the multiple-zones dialog.
        When not sent it defaults to None.
        """
        serializer = AssetTransactionSerializer(data=self._movement_payload())
        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertIsNone(serializer.validated_data["zone_id"])
