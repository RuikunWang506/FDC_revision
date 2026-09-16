import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from product_detail_module.product_detail import (
    catalog_style_from_search,
    lookup_catalog_images_for_miss,
    normalize_product_search_input,
)


class ProductDetailCatalogMissTests(unittest.TestCase):
    def test_strips_trailing_plus_and_spaces(self):
        self.assertEqual(normalize_product_search_input("N2764+"), "N2764")
        self.assertEqual(normalize_product_search_input("N2764 +"), "N2764")
        self.assertEqual(normalize_product_search_input("  N2764  "), "N2764")

    def test_style_from_search_drops_color(self):
        self.assertEqual(catalog_style_from_search("N2764 BK"), "N2764")
        self.assertEqual(catalog_style_from_search("N2764+"), "N2764")

    def test_catalog_pictures_when_oms_misses(self):
        fake = [{"url": "/image/333813", "filename": "N2764.jpg"}]
        with patch(
            "product_detail_module.product_detail.fetch_style_images",
            return_value=fake,
        ) as fetch:
            style, images = lookup_catalog_images_for_miss("N2764+")
        self.assertEqual(style, "N2764")
        self.assertEqual(images, fake)
        fetch.assert_called_once_with("N2764", max_images=12)


if __name__ == "__main__":
    unittest.main()
