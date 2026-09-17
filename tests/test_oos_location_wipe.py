import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from py.search.outofstock_check import select_sold_out_located_styles


class SelectSoldOutLocatedStylesTests(unittest.TestCase):
    def test_wipes_received_and_sold_out(self):
        located = ["GN2432", "M1141", "KEEP1"]
        stock = [
            ("GN2432", 0, 12, 0),
            ("M1141", 4, 10, -6),
            ("KEEP1", 8, 1, 7),
        ]
        self.assertEqual(
            select_sold_out_located_styles(located, stock),
            ["GN2432", "M1141"],
        )

    def test_skips_never_received_and_unlocated(self):
        located = ["NEW1"]
        stock = [
            ("NEW1", 0, 0, 0),
            ("OTHER", 0, 5, 0),
        ]
        self.assertEqual(select_sold_out_located_styles(located, stock), [])

    def test_ignores_blank_styles(self):
        located = ["  ", "AA1"]
        stock = [("AA1", 0, 3, 0), ("", 0, 9, 0)]
        self.assertEqual(select_sold_out_located_styles(located, stock), ["AA1"])


if __name__ == "__main__":
    unittest.main()
