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


headers = load_module("fdc_security_headers", "py/utilities/security_headers.py")


class SecurityHeaderTests(unittest.TestCase):
    def test_csp_allows_datatables_cdn(self):
        policy = headers.CSP_POLICY
        self.assertIn("https://cdn.datatables.net", policy)
        self.assertIn("style-src", policy)
        self.assertIn("script-src", policy)
        script_src = policy.split("script-src", 1)[1].split(";", 1)[0]
        style_src = policy.split("style-src", 1)[1].split(";", 1)[0]
        self.assertIn("https://cdn.datatables.net", script_src)
        self.assertIn("https://cdn.datatables.net", style_src)

    def test_csp_allows_cloudflare_insights(self):
        self.assertIn("https://static.cloudflareinsights.com", headers.CSP_POLICY)

    def test_csp_allows_jsdelivr_and_blob_frames(self):
        policy = headers.CSP_POLICY
        style_src = policy.split("style-src", 1)[1].split(";", 1)[0]
        script_src = policy.split("script-src", 1)[1].split(";", 1)[0]
        frame_src = policy.split("frame-src", 1)[1].split(";", 1)[0]
        self.assertIn("https://cdn.jsdelivr.net", style_src)
        self.assertIn("https://cdn.jsdelivr.net", script_src)
        self.assertIn("blob:", frame_src)


if __name__ == "__main__":
    unittest.main()
