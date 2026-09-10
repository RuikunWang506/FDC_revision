import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_module(name: str, relative: str):
    path = ROOT / relative
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


passwords = load_module("fdc_passwords", "py/utilities/passwords.py")


class PasswordTests(unittest.TestCase):
    def test_hash_and_verify(self):
        hashed = passwords.hash_password("secret-pass")
        self.assertTrue(passwords.is_hashed(hashed))
        self.assertTrue(passwords.verify_password("secret-pass", hashed))
        self.assertFalse(passwords.verify_password("wrong", hashed))

    def test_legacy_plaintext_upgrade_flag(self):
        self.assertFalse(passwords.is_hashed("ucandoit"))
        self.assertTrue(passwords.needs_rehash("ucandoit"))
        self.assertTrue(passwords.verify_password("ucandoit", "ucandoit"))
        self.assertFalse(passwords.verify_password("nope", "ucandoit"))

    def test_empty_rejected(self):
        self.assertFalse(passwords.verify_password("x", None))
        self.assertFalse(passwords.verify_password(None, "x"))


if __name__ == "__main__":
    unittest.main()
