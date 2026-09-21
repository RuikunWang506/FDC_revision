import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from py.utilities import lan_detection


class LanDetectionTests(unittest.TestCase):
    def test_office_lan_ip_only_254_subnet(self):
        self.assertTrue(lan_detection.is_office_lan_ip("192.168.254.50"))
        self.assertTrue(lan_detection.is_office_lan_ip("192.168.254.1"))
        self.assertFalse(lan_detection.is_office_lan_ip("192.168.1.50"))
        self.assertFalse(lan_detection.is_office_lan_ip("172.56.180.60"))
        self.assertFalse(lan_detection.is_office_lan_ip("192.168.254.0"))
        self.assertFalse(lan_detection.is_office_lan_ip("192.168.254.255"))

    def test_remote_login_has_no_lan_fragment(self):
        self.assertEqual(
            lan_detection.office_lan_log_fragment("172.56.180.60", "", ""),
            "",
        )

    def test_fragment_adds_lan_and_mac_beside_public_ip(self):
        self.assertEqual(
            lan_detection.office_lan_log_fragment(
                "172.56.180.60", "192.168.254.50", "aa:bb:cc:dd:ee:ff"
            ),
            "LAN: 192.168.254.50; MAC: aa:bb:cc:dd:ee:ff; ",
        )

    def test_fragment_skips_duplicate_lan_when_ip_is_already_lan(self):
        self.assertEqual(
            lan_detection.office_lan_log_fragment(
                "192.168.254.50", "192.168.254.50", "aa:bb:cc:dd:ee:ff"
            ),
            "MAC: aa:bb:cc:dd:ee:ff; ",
        )

    def test_reported_lan_ip_ignores_public_spoof(self):
        from flask import Flask, request

        app = Flask(__name__)
        with app.test_request_context("/", method="POST", data={"lan_ip": "8.8.8.8"}):
            self.assertEqual(lan_detection.reported_office_lan_ip(request), "")
            self.assertEqual(lan_detection.office_lan_ip_from_request(request), "")

    def test_reported_lan_ip_accepted_when_xff_is_cellular(self):
        from flask import Flask, request

        app = Flask(__name__)
        with app.test_request_context(
            "/",
            method="POST",
            data={"lan_ip": "192.168.254.77"},
            environ_base={"REMOTE_ADDR": "172.56.180.60"},
            headers={"X-Forwarded-For": "172.56.180.60"},
        ):
            self.assertEqual(
                lan_detection.office_lan_ip_from_request(request), "192.168.254.77"
            )

    def test_direct_lan_remote_addr(self):
        from flask import Flask, request

        app = Flask(__name__)
        with app.test_request_context(
            "/",
            method="POST",
            environ_base={"REMOTE_ADDR": "192.168.254.12"},
        ):
            self.assertEqual(
                lan_detection.office_lan_ip_from_request(request), "192.168.254.12"
            )

    def test_lookup_mac_from_attendance(self):
        with mock.patch(
            "py.utilities.attendance_store.get_device_by_lan_ip",
            return_value={"mac": "aa:bb:cc:dd:ee:01"},
        ):
            self.assertEqual(
                lan_detection.lookup_office_mac("192.168.254.29"),
                "aa:bb:cc:dd:ee:01",
            )

    def test_lookup_mac_arp_fallback(self):
        with mock.patch(
            "py.utilities.attendance_store.get_device_by_lan_ip",
            return_value=None,
        ):
            mac = lan_detection.lookup_office_mac(
                "192.168.254.9",
                arp_rows=[{"ip": "192.168.254.9", "mac": "aa:bb:cc:dd:ee:ff"}],
            )
            self.assertEqual(mac, "aa:bb:cc:dd:ee:ff")

    def test_lan_beacon_records_office_peer_not_public_host(self):
        from flask import Flask, request

        app = Flask(__name__)
        probe = "yetihost01"
        with app.test_request_context(
            f"/login/lan-beacon.png?id={probe}",
            headers={"Host": "www.fdcef.com"},
            environ_base={"REMOTE_ADDR": "192.168.254.30"},
        ):
            self.assertEqual(lan_detection.record_lan_beacon_from_request(request, probe), "")
        self.assertEqual(lan_detection.consume_lan_probe(probe), "")

        with app.test_request_context(
            f"/login/lan-beacon.png?id={probe}",
            headers={"Host": "192.168.254.30:5555"},
            environ_base={"REMOTE_ADDR": "192.168.254.30"},
        ):
            self.assertEqual(
                lan_detection.record_lan_beacon_from_request(request, probe),
                "192.168.254.30",
            )
        with app.test_request_context(
            "/",
            method="POST",
            data={"lan_probe_id": probe},
            environ_base={"REMOTE_ADDR": "172.56.180.60"},
            headers={"X-Forwarded-For": "172.56.180.60"},
        ):
            self.assertEqual(
                lan_detection.office_lan_ip_from_request(request), "192.168.254.30"
            )

    def test_lan_beacon_loopback_uses_this_host_lan_ip(self):
        from flask import Flask, request

        app = Flask(__name__)
        probe = "loopback1"
        with mock.patch(
            "py.utilities.attendance_netgear.lan_ip_of_this_host",
            return_value="192.168.254.30",
        ):
            with app.test_request_context(
                f"/login/lan-beacon.png?id={probe}",
                headers={"Host": "127.0.0.1:5555"},
                environ_base={"REMOTE_ADDR": "127.0.0.1"},
            ):
                self.assertEqual(
                    lan_detection.record_lan_beacon_from_request(request, probe),
                    "192.168.254.30",
                )
        self.assertEqual(lan_detection.consume_lan_probe(probe), "192.168.254.30")


if __name__ == "__main__":
    unittest.main()
