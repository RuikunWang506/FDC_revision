import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "fdc_smart_search", ROOT / "py/search/smart_search.py"
)
ss = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(ss)


class SmartSearchIntentTests(unittest.TestCase):
    def test_doc_number_accepts_4_and_5_digits(self):
        self.assertTrue(ss.looks_like_doc_number("18432"))
        self.assertTrue(ss.looks_like_doc_number("1843"))
        self.assertFalse(ss.looks_like_doc_number("2026"))
        self.assertFalse(ss.looks_like_doc_number("M1141"))

    def test_sku_and_container_shapes(self):
        self.assertTrue(ss.looks_like_sku("M1141"))
        self.assertTrue(ss.looks_like_sku("M1141 BLK"))
        self.assertFalse(ss.looks_like_sku("18432"))
        self.assertTrue(ss.looks_like_container("F23265"))
        self.assertTrue(ss.looks_like_container("FT25141"))
        self.assertFalse(ss.looks_like_container("Loris"))

    def test_customer_and_address_gates(self):
        self.assertTrue(ss.looks_like_customer_text("Loris"))
        self.assertTrue(ss.looks_like_address("Los Angeles"))
        self.assertTrue(ss.looks_like_address("90015"))
        self.assertFalse(ss.looks_like_address("M1141"))
        self.assertFalse(ss.looks_like_customer_text("M1141"))


class SmartSearchCategoryGateTests(unittest.TestCase):
    def test_five_digits_runs_orders_invoices_not_skus(self):
        self.assertTrue(ss.should_run_category("orders", "18432"))
        self.assertTrue(ss.should_run_category("invoices", "18432"))
        self.assertFalse(ss.should_run_category("skus", "18432"))
        self.assertFalse(ss.should_run_category("images", "18432"))
        self.assertTrue(ss.should_run_category("addresses", "18432"))

    def test_sku_runs_catalog_not_orders(self):
        self.assertTrue(ss.should_run_category("skus", "M1141"))
        self.assertTrue(ss.should_run_category("list_creator", "M1141"))
        self.assertTrue(ss.should_run_category("stock", "M1141"))
        self.assertTrue(ss.should_run_category("images", "M1141"))
        self.assertFalse(ss.should_run_category("orders", "M1141"))
        self.assertFalse(ss.should_run_category("customers", "M1141"))

    def test_name_runs_customer_and_tools(self):
        self.assertTrue(ss.should_run_category("customers", "Loris"))
        self.assertTrue(ss.should_run_category("tools", "Loris"))
        self.assertFalse(ss.should_run_category("orders", "Loris"))
        self.assertFalse(ss.should_run_category("skus", "Loris"))

    def test_short_query_skipped(self):
        self.assertFalse(ss.should_run_category("customers", "A"))


class SmartSearchFilterTests(unittest.TestCase):
    def test_skips_empty_categories(self):
        cats = ss.filter_nonempty_categories(
            [
                {"id": "orders", "items": []},
                {"id": "skus", "items": [{"title": "M1141", "href": "/x"}]},
                {"id": "customers", "items": [None, {}]},
            ]
        )
        self.assertEqual([c["id"] for c in cats], ["skus"])
        self.assertEqual(cats[0]["count"], 1)

    def test_min_query_returns_no_categories(self):
        result = ss.run_smart_search(" ")
        self.assertEqual(result["categories"], [])


if __name__ == "__main__":
    unittest.main()
