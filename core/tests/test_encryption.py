from cryptography.fernet import Fernet, InvalidToken
from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase

from core.encryption import SecretCipher


class SecretCipherTests(SimpleTestCase):
    OLD_KEY = Fernet.generate_key().decode()
    NEW_KEY = Fernet.generate_key().decode()

    def setUp(self):
        self.enterContext(self.settings(ENCRYPTION_KEYS=[self.OLD_KEY]))

    def test_round_trip_without_storing_plaintext(self):
        token = SecretCipher.encrypt("shpat_secret-token")
        self.assertNotIn("shpat_secret-token", token)
        self.assertEqual(SecretCipher.decrypt(token), "shpat_secret-token")

    def test_independent_of_secret_key(self):
        token = SecretCipher.encrypt("value")
        with self.settings(SECRET_KEY="a-rotated-secret-key"):
            self.assertEqual(SecretCipher.decrypt(token), "value")

    def test_old_key_still_decrypts_after_rotation(self):
        token = SecretCipher.encrypt("value")
        with self.settings(ENCRYPTION_KEYS=[self.NEW_KEY, self.OLD_KEY]):
            self.assertEqual(SecretCipher.decrypt(token), "value")
            rotated = SecretCipher.encrypt("value")
        with self.settings(ENCRYPTION_KEYS=[self.NEW_KEY]):
            self.assertEqual(SecretCipher.decrypt(rotated), "value")

    def test_unknown_key_is_rejected(self):
        token = SecretCipher.encrypt("value")
        with self.settings(ENCRYPTION_KEYS=[self.NEW_KEY]):
            with self.assertRaises(InvalidToken):
                SecretCipher.decrypt(token)

    def test_missing_or_invalid_keys_fail_clearly(self):
        for keys in ([], ["not-a-fernet-key"]):
            with self.subTest(keys=keys), self.settings(ENCRYPTION_KEYS=keys):
                with self.assertRaises(ImproperlyConfigured):
                    SecretCipher.encrypt("value")
