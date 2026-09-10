import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "fdc_rate_limit", ROOT / "py/utilities/rate_limit.py"
)
rate_limit = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(rate_limit)


class RateLimitTests(unittest.TestCase):
    def test_sliding_window_blocks_after_limit(self):
        limiter = rate_limit.SlidingWindowLimiter()
        self.assertFalse(limiter.too_many("ip:login", 3, 60))
        self.assertFalse(limiter.too_many("ip:login", 3, 60))
        self.assertFalse(limiter.too_many("ip:login", 3, 60))
        self.assertTrue(limiter.too_many("ip:login", 3, 60))

    def test_is_limited_does_not_record(self):
        limiter = rate_limit.SlidingWindowLimiter()
        self.assertFalse(limiter.is_limited("ip:login", 1, 60))
        self.assertFalse(limiter.is_limited("ip:login", 1, 60))
        limiter.record("ip:login")
        self.assertTrue(limiter.is_limited("ip:login", 1, 60))

    def test_client_limit_key_strips_xff_list(self):
        self.assertEqual(rate_limit.client_limit_key("1.2.3.4, 5.6.7.8", "login"), "1.2.3.4:login")


if __name__ == "__main__":
    unittest.main()
