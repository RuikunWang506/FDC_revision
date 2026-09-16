import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from py.utilities.csrf import rewrite_html_csrf_meta


class RewriteHtmlCsrfMetaTests(unittest.TestCase):
    def test_replaces_name_then_content_meta(self):
        html = (
            '<html><head>'
            '<meta name="csrf-token" content="baked-worker-token">'
            '<script src="/static/js/fdc-csrf.js"></script>'
            '</head><body></body></html>'
        )
        out = rewrite_html_csrf_meta(html, "live-session-token")
        self.assertIn('content="live-session-token"', out)
        self.assertNotIn("baked-worker-token", out)
        self.assertEqual(out.count("live-session-token"), 1)
        self.assertIn("/static/js/fdc-csrf.js?v=2", out)

    def test_replaces_content_then_name_meta(self):
        html = '<meta content="old" name="csrf-token">'
        out = rewrite_html_csrf_meta(html, "new-token")
        self.assertIn('content="new-token"', out)
        self.assertNotIn("old", out)

    def test_injects_meta_when_missing(self):
        html = "<html><head><title>x</title></head><body></body></html>"
        out = rewrite_html_csrf_meta(html, "fresh")
        self.assertIn('<meta name="csrf-token" content="fresh">', out)
        self.assertIn("/static/js/fdc-csrf.js?v=2", out)
        self.assertLess(out.index("csrf-token"), out.lower().index("</head>"))

    def test_rewrites_hidden_form_fields(self):
        html = (
            '<meta name="csrf-token" content="old">'
            '<input type="hidden" name="csrf_token" value="old">'
        )
        out = rewrite_html_csrf_meta(html, "new")
        self.assertEqual(out.count("old"), 0)
        self.assertEqual(out.count('value="new"'), 1)
        self.assertIn('content="new"', out)

    def test_bumps_csrf_script_cache_bust(self):
        html = '<script src="/static/js/fdc-csrf.js"></script>'
        out = rewrite_html_csrf_meta(
            '<meta name="csrf-token" content="old">' + html,
            "new",
        )
        self.assertIn('src="/static/js/fdc-csrf.js?v=2"', out)
        self.assertEqual(out.count("fdc-csrf.js"), 1)


if __name__ == "__main__":
    unittest.main()
