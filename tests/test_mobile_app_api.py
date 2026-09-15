import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from py.other.mobile_app_api import NATIVE_TOOL_IDS, strip_html, tools_payload
from py.utilities.csrf import CSRF_EXEMPT_ENDPOINTS
from py.utilities.dashboard_permissions import PUBLIC_ENDPOINTS


class MobileAppApiTests(unittest.TestCase):
    def test_strip_html_location_span(self):
        self.assertEqual(strip_html('<span class="editable">A-01</span>'), "A-01")
        self.assertEqual(strip_html(None), "")

    def test_native_tools(self):
        self.assertIn("item_lookup", NATIVE_TOOL_IDS)
        self.assertIn("order_lookup", NATIVE_TOOL_IDS)
        self.assertIn("bag_style_viewer", NATIVE_TOOL_IDS)
        self.assertIn("print_center", NATIVE_TOOL_IDS)
        self.assertIn("mobile_scanner", NATIVE_TOOL_IDS)
        self.assertGreaterEqual(len(NATIVE_TOOL_IDS), 12)

    def test_yeti_tools_mark_native(self):
        class _App:
            extensions = {"fdc_modules": {"loaded": {}, "failed": {}}}

        from flask import Flask

        app = Flask(__name__)
        app.extensions["fdc_modules"] = {"loaded": {}, "failed": {}}
        with app.app_context():
            tools = tools_payload("yeti", {"tools": None})
        ids = {t["id"] for t in tools}
        self.assertIn("item_lookup", ids)
        native = {t["id"] for t in tools if t["native"]}
        self.assertTrue({"item_lookup", "order_lookup"}.issubset(native))
        self.assertTrue(native.issubset(NATIVE_TOOL_IDS))

    def test_records_skip_plain_html(self):
        from py.other.mobile_app_api import _clean_inventory_row, _records_from_html_table

        self.assertEqual(_records_from_html_table("<p>No item history</p>"), [])
        row = _clean_inventory_row({"SKU": "ABC", "\U0001f6a2": 12, "Avail": 3})
        self.assertEqual(row["ocean"], 12)
        self.assertEqual(row["Avail"], 3)

    def test_login_is_csrf_and_public(self):
        self.assertIn("mobile_app.mobile_login", CSRF_EXEMPT_ENDPOINTS)
        self.assertIn("mobile_app.mobile_login", PUBLIC_ENDPOINTS)
        self.assertIn("mobile_app.mobile_session", PUBLIC_ENDPOINTS)


if __name__ == "__main__":
    unittest.main()
