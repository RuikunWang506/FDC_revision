import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from py.search.item_lookup import (
    _dataframe_has_inventory_rows,
    _lookup_item_catalog_images,
    _resolve_image_search_terms,
)


class ItemLookupEmptyStyleTests(unittest.TestCase):
    def test_empty_dataframe_is_not_inventory(self):
        self.assertFalse(_dataframe_has_inventory_rows(None))
        self.assertFalse(_dataframe_has_inventory_rows(pd.DataFrame(columns=["SKU", "Style"])))
        self.assertTrue(_dataframe_has_inventory_rows(pd.DataFrame([{"SKU": "N2458"}])))

    def test_empty_oms_result_still_searches_typed_style(self):
        terms, mode = _resolve_image_search_terms("n2764", pd.DataFrame(columns=["SKU"]))
        self.assertEqual(terms, ["n2764"])
        self.assertEqual(mode, "prefix")
        none_terms, none_mode = _resolve_image_search_terms("n2764", None)
        self.assertEqual(none_terms, ["n2764"])
        self.assertEqual(none_mode, "prefix")

    def test_lookup_returns_dam_urls_when_oms_has_no_rows(self):
        asset = MagicMock()
        asset.filename = "N2764.jpg"
        asset.file_size = 12
        asset.created_at = None
        asset.width = 100
        asset.height = 100
        asset.version = 1
        asset.is_canonical = True
        asset.sku = "N2764"
        asset.id = 333813

        db = MagicMock()
        query = db.query.return_value.filter.return_value.order_by.return_value
        query.all.return_value = [asset]

        with patch("py.search.item_lookup.py.utilities.models.SessionLocal", return_value=db):
            urls, info, names = _lookup_item_catalog_images("n2764", None)

        self.assertEqual(urls, ["/image/333813/300"])
        self.assertEqual(names, ["N2764.jpg"])
        self.assertTrue(info)
        db.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
