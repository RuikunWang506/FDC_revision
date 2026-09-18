import importlib.util
import re
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

    def test_name_runs_customer_orders_and_invoices(self):
        self.assertTrue(ss.should_run_category("customers", "Loris"))
        self.assertTrue(ss.should_run_category("customer_skus", "Loris"))
        self.assertTrue(ss.should_run_category("orders", "Loris"))
        self.assertTrue(ss.should_run_category("invoices", "Loris"))
        self.assertTrue(ss.should_run_category("order_processor", "Loris"))
        self.assertTrue(ss.should_run_category("tools", "Loris"))
        self.assertTrue(ss.should_run_category("skus", "Loris"))
        self.assertTrue(ss.should_run_category("descriptions", "Loris"))

    def test_location_and_bag_style_gates(self):
        self.assertTrue(ss.looks_like_location("L1-A BOT"))
        self.assertTrue(ss.should_run_category("locations", "M1141"))
        self.assertTrue(ss.should_run_category("bag_styles", "M1141"))
        self.assertTrue(ss.should_run_category("item_lookup", "M1141"))

    def test_short_query_skipped(self):
        self.assertFalse(ss.should_run_category("customers", "A"))

    def test_four_digits_runs_skus_and_customer_id(self):
        self.assertTrue(ss.looks_like_style_digits("1141"))
        self.assertFalse(ss.looks_like_style_digits("18432"))
        self.assertTrue(ss.should_run_category("skus", "1141"))
        self.assertTrue(ss.should_run_category("bag_styles", "1141"))
        self.assertTrue(ss.should_run_category("customers", "1141"))
        self.assertFalse(ss.should_run_category("skus", "18432"))
        where_sql, params = ss._customer_match_sql("1141")
        self.assertIn("cus_id", where_sql)
        self.assertIn("PHONE", where_sql)
        self.assertNotIn("ADDRESS", where_sql)
        self.assertIn("01141", params)
        self.assertIn("%1141", params)
        self.assertIn("%1141%", params)

    def test_style_words_run_catalog_not_orders(self):
        for word in ("satchel", "crossbody"):
            self.assertTrue(ss.looks_like_style_word(word))
            self.assertTrue(ss.should_run_category("descriptions", word))
            self.assertTrue(ss.should_run_category("skus", word))
            self.assertTrue(ss.should_run_category("bag_styles", word))
            self.assertTrue(ss.should_run_category("item_lookup", word))
            self.assertFalse(ss.should_run_category("images", word))
        self.assertFalse(ss.looks_like_style_word("M1141"))
        self.assertFalse(ss.looks_like_style_word("18432"))


class SmartSearchPatternTests(unittest.TestCase):
    def test_wildcard_contains_matches_t3305(self):
        pat = ss.compile_search_pattern("%3305%")
        self.assertTrue(pat.has_wildcard)
        self.assertEqual(pat.like, "%3305%")
        self.assertEqual(pat.core, "3305")
        self.assertTrue(re.search(pat.regex, "T3305", re.I))
        self.assertTrue(ss.should_run_category("skus", "%3305%"))

    def test_star_and_question_become_like(self):
        pat = ss.compile_search_pattern("T33*")
        self.assertEqual(pat.like, "T33%")
        self.assertTrue(re.search(pat.regex, "T3305"))
        qpat = ss.compile_search_pattern("T330?")
        self.assertEqual(qpat.like, "T330_")
        self.assertTrue(re.search(qpat.regex, "T3305"))

    def test_slash_regex_mode(self):
        pat = ss.compile_search_pattern(r"/T33\d+/")
        self.assertTrue(pat.is_regex)
        self.assertEqual(pat.core, "T33")
        self.assertTrue(ss.should_run_category("skus", r"/T33\d+/"))

    def test_phone_digits_run_customers_not_orders(self):
        self.assertTrue(ss.looks_like_phone("5858721823"))
        self.assertTrue(ss.looks_like_phone("(585) 872-1823"))
        self.assertTrue(ss.should_run_category("customers", "5858721823"))
        self.assertFalse(ss.should_run_category("orders", "5858721823"))
        self.assertFalse(ss.should_run_category("skus", "5858721823"))

    def test_area_code_searches_phone_not_just_customer_id(self):
        self.assertFalse(ss.looks_like_phone("626"))
        self.assertTrue(ss.should_run_category("customers", "626"))
        where_sql, params = ss._customer_match_sql("626")
        self.assertIn("cus_id", where_sql)
        self.assertIn("PHONE", where_sql)
        self.assertNotIn("ADDRESS", where_sql)
        self.assertIn("%626%", params)
        self.assertTrue(ss._text_matches("626", "(626) 555-1212"))
        self.assertTrue(ss._text_matches("6268721823", "(626) 872-1823"))


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

    def test_pick_match_exposes_field(self):
        field, text = ss._pick_match(
            "1141",
            (("Style", "L1397"), ("Description", "F191141 Satchel")),
        )
        self.assertEqual(field, "Description")
        self.assertIn("1141", text)
        item = ss._with_match(
            {"title": "N1666 BK", "subtitle": "FASHION Q #788", "href": "/x"},
            "1141",
            (("Customer ID", "01141"), ("Customer", "FASHION Q #788")),
        )
        self.assertEqual(item["match_in"], "Customer ID")
        self.assertEqual(item["match_text"], "01141")
        self.assertEqual(ss.MAX_RESULTS, 100)

    def test_money_queries(self):
        self.assertTrue(ss.looks_like_money("3784"))
        self.assertTrue(ss.looks_like_money("12.5"))
        self.assertTrue(ss.looks_like_money("$12.50"))
        self.assertFalse(ss.looks_like_money("12"))
        self.assertFalse(ss.looks_like_money("M1141"))
        self.assertEqual(ss.money_value("12.5"), 12.5)
        self.assertTrue(ss.should_run_category("orders", "3784"))
        self.assertTrue(ss.should_run_category("orders", "12.5"))
        self.assertTrue(ss.should_run_category("invoices", "12.5"))
        self.assertFalse(ss.should_run_category("customers", "12.5"))
        self.assertTrue(ss.should_run_category("skus", "12.5"))
        self.assertFalse(ss.should_run_category("skus", "18432"))

    def test_valid_order_flag(self):
        self.assertTrue(ss.has_valid_order("29729"))
        self.assertFalse(ss.has_valid_order(""))
        self.assertFalse(ss.has_valid_order("0"))
        self.assertFalse(ss.has_valid_order(None))


if __name__ == "__main__":
    unittest.main()
