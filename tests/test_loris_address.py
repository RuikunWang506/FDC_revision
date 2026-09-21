import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from py.loris.lori_automation import extract_clean_address
from py.loris.loris_address_check import (
    CONFIRM_CONFIDENCE,
    addresses_match,
    apply_address_guard,
    apply_zip_guard,
    build_prompt,
    computer_label,
    elapsed_label,
    format_internal_address,
    interpret_model_reply,
)


def _ship_block(body):
    return f"P.O. #: 0018-005435\nSHIP TO :\n{body}\nShip to Arrive: 01/01/2026\n"


class ExtractCleanAddressTests(unittest.TestCase):
    def test_five_digit_zip_on_its_own_line_is_kept(self):
        text = _ship_block(
            "0018\n"
            "Texas Health (Plano)\n"
            "6200 W Parker Rd\n"
            "Plano,\n"
            "75093\n"
            "US\n"
            "Attn: Lori's Gifts\n"
            "TX\n"
        )
        self.assertEqual(
            extract_clean_address(text),
            "Texas Health (Plano)\n6200 W Parker Rd\nPlano, TX 75093",
        )

    def test_nine_digit_zip_becomes_zip_plus_four(self):
        text = _ship_block(
            "0015\n"
            "Medical City (Fort Worth)\n"
            "900 8th Ave\n"
            "Fort Worth,\n"
            "761043907\n"
            "US\n"
            "Attn: Lori's Gifts\n"
            "TX\n"
        )
        self.assertEqual(
            extract_clean_address(text),
            "Medical City (Fort Worth)\n900 8th Ave\nFort Worth, TX 76104-3907",
        )

    def test_store_number_is_not_used_as_zip(self):
        text = _ship_block(
            "0018\n"
            "Texas Health (Plano)\n"
            "6200 W Parker Rd\n"
            "Plano, TX\n"
        )
        address = extract_clean_address(text)
        self.assertNotIn("0018", address)
        self.assertIn("Plano, TX", address)


class AddressAiReplyTests(unittest.TestCase):
    def test_confirm_and_wrong_and_fail(self):
        self.assertEqual(interpret_model_reply('{"match": true, "message": "Same hospital."}')[0], "confirm")
        self.assertEqual(interpret_model_reply('{"match": false, "message": "Different ZIP."}')[0], "wrong")
        self.assertEqual(interpret_model_reply("not json")[0], "fail")
        self.assertEqual(interpret_model_reply('{"match": "yes", "message": "Same street."}')[0], "confirm")

    def test_reads_confidence(self):
        status, message, confidence = interpret_model_reply(
            '{"match": true, "confidence": 96, "message": "Same street and city."}'
        )
        self.assertEqual(status, "confirm")
        self.assertEqual(message, "Same street and city.")
        self.assertEqual(confidence, 96)
        self.assertIsNone(interpret_model_reply('{"match": true, "message": "Same."}')[2])

    def test_strips_json_fence(self):
        status, message, confidence = interpret_model_reply(
            '```json\n{"match": true, "confidence": 91, "message": "Same place."}\n```'
        )
        self.assertEqual(status, "confirm")
        self.assertEqual(message, "Same place.")
        self.assertEqual(confidence, 91)

    def test_labels(self):
        self.assertEqual(elapsed_label(840), "840 ms")
        self.assertEqual(elapsed_label(2400), "2.4 s")
        self.assertIn("inside computer", computer_label("http://192.168.254.20:11434"))
        self.assertIn("this computer", computer_label("http://127.0.0.1:11434"))

    def test_prompt_includes_both_addresses(self):
        text = build_prompt("6200 W Parker Rd\nPlano, TX", "6200 W PARKER RD\nPLANO TX 75093")
        self.assertIn("SHIP TO:", text)
        self.assertIn("6200 W Parker Rd", text)
        self.assertIn("INTERNAL:", text)
        self.assertIn("75093", text)

    def test_zip_guard_blocks_false_confirm(self):
        status, message = apply_zip_guard(
            "confirm",
            "The addresses match.",
            "Bristol, CT 06011",
            "BRISTOL CT 06010",
        )
        self.assertEqual(status, "wrong")
        self.assertIn("06011", message)
        self.assertIn("06010", message)
        kept = apply_zip_guard(
            "confirm",
            "Same place.",
            "Plano, TX",
            "PLANO TX 75093",
        )
        self.assertEqual(kept[0], "confirm")

    def test_confidence_threshold_and_street_lines(self):
        ship = "Memorial Health (Savannah)\n4700 Waters Avenue\nSavannah, GA 31404"
        internal = (
            "MEMORIAL HEALTH (SAVANNAH)\n4700 WATERS AVE\n"
            "Lori's Gift Shop #0649\nSAVANNAH GA 31404-6220\nPhone: (972)759-5000"
        )
        status, _message, score = apply_address_guard("confirm", "Same hospital.", 96, ship, internal)
        self.assertEqual(status, "confirm")
        self.assertEqual(score, 96)

        zip4_status, zip4_message, zip4_score = apply_address_guard(
            "wrong",
            "ZIP codes do not match",
            0,
            "UofL (Mary and Elizabeth)\n1850 Bluegrass Ave.\nLouisville, KY 40215",
            "UOFL (MARY AND ELIZABETH)\n1850 BLUEGRASS AVE\nLori's Gift Shop #0661\n"
            "LOUISVILLE KY 40215-1161\nPhone: (972)759-5000",
        )
        self.assertEqual(zip4_status, "confirm")
        self.assertGreaterEqual(zip4_score, CONFIRM_CONFIDENCE)
        self.assertIn("4 digits", zip4_message)

        typo_status, _typo_message, typo_score = apply_address_guard(
            "wrong",
            "city mismatch",
            0,
            "University of Virginia (Universi\n1215 Lee Street\nCharlottesville, VA 22903",
            "UNIVERSITY OF VIRGINIA (UNIVERSI\n1215 LEE ST\nLori's Gift Shop #0660\n"
            "CHARLOTTESVLE VA 22903\nPhone: (972)759-5000",
        )
        self.assertEqual(typo_status, "confirm")
        self.assertGreaterEqual(typo_score, CONFIRM_CONFIDENCE)

        fragment_status, _, _ = apply_address_guard(
            "wrong",
            "city mismatch",
            0,
            "LifeBridge (Northwest Hospital)\n5401 OLD COURT RD\nN, MD 21133",
            "LIFEBRIDGE (NORTHWEST HOSPITAL)\n5401 OLD COURT RD\nLori's Gift Shop #0668\n"
            "RANDALLSTOWN MD 21133-5103\nPhone: (972)759-5000",
        )
        self.assertEqual(fragment_status, "confirm")

        abbrev = apply_address_guard(
            "wrong",
            "street mismatch",
            0,
            "Clinic\n6200 SW Freeway\nHouston, TX 77074",
            "CLINIC\n6200 SW FWY\nLori's Gift Shop\nHOUSTON TX 77074-1234",
        )
        self.assertEqual(abbrev[0], "confirm")

        bellevue = apply_address_guard(
            "wrong",
            "street mismatch",
            0,
            "NYC Health (Bellevue)\n462 First Ave 27th St\nNew York, NY 10016",
            "NYC HEALTH (BELLEVUE)\n462 1ST AVE FRNT 1 # 27\nLori's Gift Shop #0123\n"
            "NEW YORK NY 10016-9196\nPhone: (212) 562-4246",
        )
        self.assertEqual(bellevue[0], "confirm")
        cross_street = apply_address_guard(
            "confirm",
            "same",
            99,
            "Clinic\n100 Broadway 165th St\nNew York, NY 10032",
            "CLINIC\n100 BROADWAY\nNEW YORK NY 10032",
        )
        self.assertEqual(cross_street[0], "wrong")

        low_status, low_message, low_score = apply_address_guard(
            "confirm", "Maybe the same.", 55, "Plano, TX", "PLANO TX 75093"
        )
        self.assertEqual(low_status, "wrong")
        self.assertEqual(low_score, 55)
        self.assertIn(str(CONFIRM_CONFIDENCE), low_message)

        mismatch = apply_address_guard(
            "confirm",
            "ZIP matches.",
            99,
            "Other Clinic\n1200 Abercorn St\nSavannah, GA 31404",
            internal,
        )
        self.assertEqual(mismatch[0], "wrong")
        self.assertIn("1200", mismatch[1])
        self.assertIn("4700", mismatch[1])
        self.assertLess(mismatch[2], CONFIRM_CONFIDENCE)

    def test_past_shipment_matches_internal_address(self):
        internal = (
            "NYC HEALTH (BELLEVUE)\n462 1ST AVE FRNT 1 # 27\n"
            "NEW YORK NY 10016-9196"
        )
        same = "NYC Health (Bellevue)\n462 First Ave 27th St\nNew York, NY 10016"
        other = "Other Clinic\n100 Broadway\nNew York, NY 10016"
        self.assertTrue(addresses_match(same, internal))
        self.assertFalse(addresses_match(other, internal))
        self.assertFalse(addresses_match("", internal))

    def test_internal_address_layout(self):
        text = format_internal_address({
            "company": "Texas Health",
            "address1": "6200 W Parker Rd",
            "address2": "",
            "address3": "",
            "city": "PLANO",
            "state": "TX",
            "zip_code": "75093-7939",
            "phone": "2145551212",
        })
        self.assertEqual(
            text,
            "Texas Health\n6200 W Parker Rd\nPLANO TX 75093-7939\nPhone: 214-555-1212",
        )


if __name__ == "__main__":
    unittest.main()
