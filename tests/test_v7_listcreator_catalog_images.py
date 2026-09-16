import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from py.Toolkits.V7listcreator import (
    LISTCREATOR_STYLE_TOOLS_JS,
    _inject_listcreator_style_tools_js,
    _normalize_requested_styles,
    listcreator_catalog_images_for_styles,
)


class NormalizeRequestedStylesTests(unittest.TestCase):
    def test_strips_dedupes_and_keeps_first_case(self):
        self.assertEqual(
            _normalize_requested_styles([" m1141 ", "M1141", "TBDBN3602"]),
            ["m1141", "TBDBN3602"],
        )

    def test_rejects_markup_and_blank(self):
        self.assertEqual(
            _normalize_requested_styles(["", None, "<script>", "ok-style"]),
            ["ok-style"],
        )


class InjectStyleToolsTests(unittest.TestCase):
    def test_cached_html_uses_current_style_tools_version(self):
        html = '<html><body><script src="/static/js/v7_listcreator_style_tools.js?v=3"></script></body></html>'
        out = _inject_listcreator_style_tools_js(html)
        self.assertIn(LISTCREATOR_STYLE_TOOLS_JS, out)
        self.assertIn("v=4", LISTCREATOR_STYLE_TOOLS_JS)
        self.assertNotIn("v=3", out)


class CatalogImagesForStylesTests(unittest.TestCase):
    def test_maps_found_images_to_requested_style(self):
        with patch(
            "py.Toolkits.V7listcreator.get_images_for_selection",
            return_value={"M1141": [{"path": "/image/1/150", "fullsize_path": "/image/1/1800"}]},
        ):
            out = listcreator_catalog_images_for_styles(["m1141"])
        self.assertEqual(out["m1141"][0]["path"], "/image/1/150")
        self.assertEqual(out["M1141"][0]["fullsize_path"], "/image/1/1800")


if __name__ == "__main__":
    unittest.main()
