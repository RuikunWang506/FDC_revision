import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class LocaleTests(unittest.TestCase):
    def test_en_zh_key_parity(self):
        en = json.loads((ROOT / "locales" / "en.json").read_text(encoding="utf-8"))
        zh = json.loads((ROOT / "locales" / "zh.json").read_text(encoding="utf-8"))
        self.assertEqual(set(en), set(zh))

    def test_required_new_keys_present(self):
        en = json.loads((ROOT / "locales" / "en.json").read_text(encoding="utf-8"))
        for key in (
            "dashboard.title",
            "page.print_center.title",
            "tool.print_center.title",
            "tool.print_center.desc",
            "error.404.title",
            "error.500.title",
            "page.manage_users.title",
            "dashboard.quick_access",
            "dashboard.search_tools",
            "dashboard.no_match",
            "tool.smart_search.title",
            "page.smart_search.title",
            "tool.box_price_checker.title",
            "page.box_price_checker.title",
            "tool.mini_tools.title",
            "page.mini_tools.title",
            "tool.attendance_log.title",
            "page.attendance_log.title",
            "bulletin.title",
            "tool.bulletin.title",
        ):
            self.assertIn(key, en)
            self.assertTrue(en[key])


if __name__ == "__main__":
    unittest.main()
