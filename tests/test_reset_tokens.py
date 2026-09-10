import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "fdc_reset_tokens", ROOT / "py/utilities/reset_tokens.py"
)
reset_tokens = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(reset_tokens)


class ResetTokenTests(unittest.TestCase):
    def test_hash_is_sha256_hex(self):
        digest = reset_tokens.hash_reset_token("abc")
        self.assertEqual(len(digest), 64)
        self.assertEqual(digest, reset_tokens.hash_reset_token("abc"))
        self.assertNotEqual(digest, "abc")
        self.assertNotEqual(digest, reset_tokens.hash_reset_token("abd"))

    def test_empty_token_still_hashes(self):
        self.assertEqual(len(reset_tokens.hash_reset_token("")), 64)
        self.assertEqual(len(reset_tokens.hash_reset_token(None)), 64)


if __name__ == "__main__":
    unittest.main()
