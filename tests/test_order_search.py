import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from py.order.order_search import _order_ship_context, _ship_to_display_lines


class ShipToDisplayTests(unittest.TestCase):
    def test_includes_oms_address_2_overflow(self):
        lines = _ship_to_display_lines(
            "",
            "WONDERFUL WORLD LTD",
            "C/O INTEGRITY LOGISTICS & SHIPPING",
            "#10301 NW 108TH AVENUE, SUITE 2",
            "MEDLEY, FL 33178",
            "MEDLEY",
            "FL",
            "33178",
            "",
        )
        self.assertEqual(
            lines,
            [
                "WONDERFUL WORLD LTD",
                "C/O INTEGRITY LOGISTICS & SHIPPING",
                "#10301 NW 108TH AVENUE, SUITE 2",
                "MEDLEY, FL 33178",
            ],
        )

    def test_skips_duplicate_address_22(self):
        lines = _ship_to_display_lines(
            "Acme",
            "123 Main St",
            "Suite 2",
            "Suite 2",
            "",
            "Medley",
            "FL",
            "33178",
            "",
        )
        self.assertEqual(lines, ["Acme", "123 Main St", "Suite 2", "Medley, FL 33178"])

    def test_gofo_street_prefers_address_22(self):
        ship = _order_ship_context(
            {
                "ship_customer_name": "WONDERFUL WORLD LTD",
                "ship_address_1": "WONDERFUL WORLD LTD",
                "ship_address_2": "C/O INTEGRITY LOGISTICS & SHIPPING",
                "ship_address_22": "#10301 NW 108TH AVENUE, SUITE 2",
                "ship_to_city": "MEDLEY",
                "ship_to_state": "FL",
                "ship_to_zip": "33178",
                "ship_to_country": "",
                "phone": "N/A",
            }
        )
        self.assertEqual(ship["street"], "#10301 NW 108TH AVENUE, SUITE 2")
        self.assertEqual(ship["city"], "MEDLEY")


if __name__ == "__main__":
    unittest.main()
