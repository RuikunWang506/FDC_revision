import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from py.other.box_price_checker import (
    match_offers,
    parse_eightnet_catalog,
    parse_inch_value,
    parse_lwh,
    parse_uline_catalog,
    parse_uline_detail,
)

ULINE_ROW = """
<tr align='center'>
<td background='#FFFFFF' class='bodytext1' nowrap><b>
<a href='/Product/ProductDetail.aspx?model=S-4121'>S-4121</a></b></td>
<td align='left' background='#FFFFFF' class='bodytext1' nowrap><b>&nbsp;<a href='/Product/ProductDetail.aspx?model=S-4121'  nowrap>12 x 8 x 6" Corrugated Boxes</a></b></td>
<td background='#FFFFFF' nowrap class='bodytext1_sale'><b>0.40</b></td>
</tr>
"""

ULINE_DETAIL = """
<td class='ChartheadLargeItemW10H18'><attrib a="947" l="10" type="2">20</attrib></td>
<td class='ChartheadLargeItemW10H18'><attrib a="948" l="10" type="2">100</attrib></td>
<td class='ChartheadLargeItemW10H18'><attrib a="949" l="10" type="2">240</attrib></td>
<td class='ChartheadLargeItemW10H18'><attrib a="950" l="10" type="2">500</attrib></td>
<td class='ChartheadLargeItemW10H18'><attrib a="951" l="10" type="2">1,000+</attrib></td>
<td>>S-13322</attrib></td>
<td class='ChartcopyItemW10H18'><attrib type="2">22</attrib> x&nbsp;<attrib type="2">16</attrib> x&nbsp;<attrib type="2">10"</attrib></td>
<td class='PriceCellChartcopyItemW10H18'><attrib>$3.48</attrib></td>
<td class='PriceCellChartcopyItemW10H18'><attrib>$3.15</attrib></td>
<td class='PriceCellChartcopyItemW10H18'><attrib>$3.02</attrib></td>
<td class='PriceCellChartcopyItemW10H18'><attrib>$2.75</attrib></td>
<td class='PriceCellChartcopyItemW10H18'><attrib>$2.59</attrib></td>
<td class='ChartcopyItemW10H18'><attrib type="2">20</attrib> / <attrib type="2">240</attrib></td>
"""

ULINE_DETAIL_SALE = """
<td class='ChartheadLargeItemW10H18'><attrib a="947" l="10" type="2">20</attrib></td>
<td class='ChartheadLargeItemW10H18'><attrib a="948" l="10" type="2">100</attrib></td>
<td class='ChartheadLargeItemW10H18'><attrib a="949" l="10" type="2">240</attrib></td>
<td class='ChartheadLargeItemW10H18'><attrib a="950" l="10" type="2">500</attrib></td>
<td class='ChartheadLargeItemW10H18'><attrib a="951" l="10" type="2">1,000+</attrib></td>
<td>>S-4642</attrib></td>
<td class='PriceCellChartcopyLargeItemRedW10H18'><attrib>$2.65</attrib></td>
<td class='PriceCellChartcopyLargeItemRedW10H18'><attrib>$2.65</attrib></td>
<td class='PriceCellChartcopyLargeItemRedW10H18'><attrib>$2.65</attrib></td>
<td class='PriceCellChartcopyLargeItemRedW10H18'><attrib>$2.65</attrib></td>
<td class='PriceCellChartcopyLargeItemRedW10H18'><attrib>$2.65</attrib></td>
<td class='ChartcopyLargeItemRedW10H18'><attrib type="2">20</attrib> / <attrib type="2">240</attrib></td>
"""

EIGHTNET_ROW = """
<tr>
<td class="index">57</td>
<td class="col model align-left cb220-160100e3s ">
CB220-160100E3S
</td>
<td class="col inside_dim">
22 x 16 x 10"
</td>
<td class="col description">
<div data-content-type="html">32 ECT S/W RSC</div>
</td>
<td class="align-right-25">2.15</td>
<td class="align-right-25">1.95</td>
<td class="align-right-25">1.68</td>
<td class="align-right-25">1.61</td>
<td class="same-price-label label">Email for Quote</td>
<td class="col qty_bndl">25</td>
<td class="col qty_skid">250</td>
</tr>
"""


class BoxPriceCheckerTests(unittest.TestCase):
    def test_parse_inch_fractions_and_ranges(self):
        self.assertEqual(parse_inch_value("12"), 12.0)
        self.assertEqual(parse_inch_value("11 1/4"), 11.25)
        self.assertEqual(parse_inch_value("3-1/2"), 3.5)
        self.assertEqual(parse_inch_value("48 - 90"), 48.0)
        self.assertEqual(parse_inch_value('8.5"'), 8.5)

    def test_parse_lwh_plain_and_labeled(self):
        self.assertEqual(parse_lwh('12 x 8 x 6" Corrugated Boxes'), (12.0, 8.0, 6.0))
        self.assertEqual(
            parse_lwh('6" L X 6" W X 4" D (Cup & Saucer) / 100/Pack'),
            (6.0, 6.0, 4.0),
        )

    def test_parse_uline_row(self):
        offers = parse_uline_catalog(ULINE_ROW)
        self.assertEqual(len(offers), 1)
        offer = offers[0]
        self.assertEqual(offer["sku"], "S-4121")
        self.assertEqual(offer["dims"], [12.0, 8.0, 6.0])
        self.assertEqual(offer["unit_price"], 0.40)
        self.assertIn("12 x 8 x 6", offer["title"])
        self.assertIn("S-4121", offer["url"])

    def test_parse_uline_detail_tiers(self):
        detail = parse_uline_detail(ULINE_DETAIL, "S-13322")
        self.assertEqual(detail["bundle_qty"], 20)
        self.assertEqual(detail["bale_qty"], 240)
        self.assertEqual(detail["unit_price"], 3.48)
        qty = [t["qty"] for t in detail["price_tiers"]]
        prices = [t["price"] for t in detail["price_tiers"]]
        self.assertEqual(qty, ["20", "100", "240", "500", "1,000+"])
        self.assertEqual(prices, [3.48, 3.15, 3.02, 2.75, 2.59])

    def test_parse_uline_detail_red_sale_cells(self):
        detail = parse_uline_detail(ULINE_DETAIL_SALE, "S-4642")
        self.assertEqual(detail["bundle_qty"], 20)
        self.assertEqual(detail["bale_qty"], 240)
        self.assertEqual([t["price"] for t in detail["price_tiers"]], [2.65, 2.65, 2.65, 2.65, 2.65])
        self.assertEqual([t["qty"] for t in detail["price_tiers"]], ["20", "100", "240", "500", "1,000+"])

    def test_parse_eightnet_row_tiers(self):
        offers = parse_eightnet_catalog(
            EIGHTNET_ROW,
            "https://www.8net.com/shipping-corrugated-boxes-bins-wraps-pads/corrugated-boxes/over-500-box-1/corrugated-box-20-23.html",
        )
        self.assertEqual(len(offers), 1)
        offer = offers[0]
        self.assertEqual(offer["sku"], "CB220-160100E3S")
        self.assertEqual(offer["dims"], [22.0, 16.0, 10.0])
        self.assertEqual(offer["unit_price"], 2.15)
        self.assertEqual(offer["bundle_qty"], 25)
        self.assertEqual(offer["skid_qty"], 250)
        self.assertIn("32 ECT", offer["description"])
        self.assertTrue(offer["url"].endswith("?a=CB220-160100E3S"))
        self.assertEqual(len(offer["price_tiers"]), 5)
        self.assertEqual(offer["price_tiers"][3]["price"], 1.61)
        self.assertIsNone(offer["price_tiers"][4]["price"])
        self.assertIn("Quote", offer["price_tiers"][4]["note"])

    def test_match_offers_ignores_orientation(self):
        offers = [
            {"sku": "A", "dims": [6.0, 12.0, 8.0], "unit_price": 1.0, "title": "rotated"},
            {"sku": "B", "dims": [12.0, 8.0, 6.4], "unit_price": 0.5, "title": "close"},
        ]
        exact = match_offers([12, 8, 6], offers, tolerance=0.5)
        self.assertEqual(exact[0]["sku"], "A")
        self.assertTrue(exact[0]["exact"])
        close_only = match_offers([12, 8, 6], [offers[1]], tolerance=0.5)
        self.assertEqual(close_only[0]["sku"], "B")
        self.assertFalse(close_only[0]["exact"])
        none = match_offers([12, 8, 6], [offers[1]], tolerance=0.25)
        self.assertEqual(none, [])
        nearest = match_offers([12, 8, 6], [offers[1]], tolerance=0.25, fallback_nearest=2)
        self.assertEqual(nearest[0]["sku"], "B")
        ordered = match_offers(
            [12, 8, 6],
            [
                {"sku": "rotated", "dims": [12.0, 6.0, 8.0], "unit_price": 0.4, "title": "12x6x8"},
                {"sku": "same", "dims": [12.0, 8.0, 6.0], "unit_price": 0.4, "title": "12x8x6"},
            ],
            tolerance=0.5,
        )
        self.assertEqual(ordered[0]["sku"], "same")

    def test_tool_wired_in_permissions_and_locales(self):
        from py.utilities.dashboard_permissions import (
            DASHBOARD_TOOL_NAV,
            DASHBOARD_TOOLS,
            TOOL_BY_BLUEPRINT,
        )

        self.assertEqual(TOOL_BY_BLUEPRINT["box_price_checker"], "box_price_checker")
        self.assertTrue(any(t["id"] == "box_price_checker" for t in DASHBOARD_TOOLS))
        self.assertEqual(DASHBOARD_TOOL_NAV["box_price_checker"]["href"], "/box_price_checker")
        en = json.loads((ROOT / "locales" / "en.json").read_text(encoding="utf-8"))
        zh = json.loads((ROOT / "locales" / "zh.json").read_text(encoding="utf-8"))
        self.assertEqual(set(en), set(zh))
        self.assertTrue(en["tool.box_price_checker.title"])
        self.assertTrue(en["page.box_price_checker.qty"])
        self.assertNotIn("Paper Mart", en["tool.box_price_checker.desc"])


if __name__ == "__main__":
    unittest.main()
