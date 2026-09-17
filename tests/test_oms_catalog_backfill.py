import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from py.utilities.oms_catalog_backfill import (
    merge_active_oms_styles,
    normalize_oms_style,
    run_oms_dropbox_catalog_backfill,
)
from py.standalone.backfill_catalog_from_dropbox import run_backfill_for_skus


class NormalizeOmsStyleTests(unittest.TestCase):
    def test_uppercases_and_strips(self):
        self.assertEqual(normalize_oms_style(" gn2432 "), "GN2432")

    def test_uses_first_token_when_sku_has_color(self):
        self.assertEqual(normalize_oms_style("GN2432 BLACK"), "GN2432")

    def test_rejects_blank_and_junk(self):
        self.assertIsNone(normalize_oms_style(""))
        self.assertIsNone(normalize_oms_style(None))
        self.assertIsNone(normalize_oms_style("<script>"))


class MergeActiveOmsStylesTests(unittest.TestCase):
    def test_skips_sold_out_keeps_stock_or_ocean(self):
        fdc = [
            ("GN2432", 4, 0),
            ("OLD999", 0, 0),
            ("OCEAN1", 0, 12),
        ]
        ef = [
            ("OLD999", 0, 0),
            ("EFONLY", 2, 0),
            ("GN2432", 0, 3),
        ]
        self.assertEqual(
            merge_active_oms_styles(fdc, ef),
            ["EFONLY", "GN2432", "OCEAN1"],
        )

    def test_sold_out_in_one_db_still_active_if_other_has_qty(self):
        self.assertEqual(
            merge_active_oms_styles([("M1141", 0, 0)], [("M1141", 1, 0)]),
            ["M1141"],
        )


class OmsBackfillRunTests(unittest.TestCase):
    def test_run_uses_active_oms_styles_only(self):
        logs = []
        with patch(
            "py.utilities.oms_catalog_backfill.fetch_active_oms_styles",
            return_value=["GN2432", "M1141"],
        ), patch(
            "py.standalone.backfill_catalog_from_dropbox.run_backfill_for_skus",
            return_value={"oms_copied": 1, "oms_missing_catalog": 2, "oms_no_dropbox": 0},
        ) as backfill:
            stats = run_oms_dropbox_catalog_backfill(apply=True, log_fn=logs.append)
        backfill.assert_called_once()
        self.assertEqual(backfill.call_args.args[0], ["GN2432", "M1141"])
        self.assertEqual(stats["oms_active_styles"], 2)
        self.assertEqual(stats["oms_copied"], 1)

    def test_no_missing_skus_skips_dropbox_walk(self):
        with patch(
            "py.standalone.backfill_catalog_from_dropbox.skus_missing_catalog",
            return_value=[],
        ), patch(
            "py.standalone.backfill_catalog_from_dropbox.dropbox_roots",
        ) as roots:
            stats = run_backfill_for_skus(["GN2432"], apply=True, log_fn=lambda *_: None)
        roots.assert_not_called()
        self.assertEqual(stats["oms_wanted"], 1)
        self.assertEqual(stats["oms_missing_catalog"], 0)
        self.assertEqual(stats["oms_copied"], 0)


if __name__ == "__main__":
    unittest.main()
