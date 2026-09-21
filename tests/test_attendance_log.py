import json
import sys
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from py.utilities.attendance_netgear import (
    merge_device_lists,
    normalize_mac,
    parse_arp_table,
    parse_at_delimited_devices,
    parse_netgear_device_xml,
    prefer_hostname,
)
from py.utilities.attendance_store import (
    classify_auto_role,
    guess_device_type,
    is_night,
    is_phone_notify_candidate,
    night_bucket_for,
    session_hour_ranges_for_day,
    session_overlap_seconds,
)
from py.utilities.dashboard_permissions import (
    ATTENDANCE_LOG_USERS,
    DASHBOARD_TOOL_NAV,
    DASHBOARD_TOOLS,
    TOOL_BY_BLUEPRINT,
    tool_visible_for_user,
)


TZ = ZoneInfo("America/Los_Angeles")


class AttendanceHelperTests(unittest.TestCase):
    def test_prefer_hostname_keeps_richer_lan_name(self):
        self.assertEqual(prefer_hostname("IPHONE", "iPhone-de-Ting"), "iPhone-de-Ting")
        self.assertEqual(prefer_hostname("iPhone-de-Ting", "IPHONE"), "iPhone-de-Ting")
        self.assertEqual(prefer_hostname("", "tristen.local."), "tristen")
        self.assertEqual(prefer_hostname("android", "LiveRoom1-PC"), "LiveRoom1-PC")

    def test_session_hour_ranges_for_day_clips_and_orders(self):
        day = datetime(2026, 9, 17, tzinfo=TZ)
        sessions = [
            (
                datetime(2026, 9, 17, 9, 15, tzinfo=TZ),
                datetime(2026, 9, 17, 12, 0, tzinfo=TZ),
            ),
            (
                datetime(2026, 9, 17, 13, 30, tzinfo=TZ),
                datetime(2026, 9, 17, 17, 45, tzinfo=TZ),
            ),
            (
                datetime(2026, 9, 16, 22, 0, tzinfo=TZ),
                datetime(2026, 9, 17, 1, 0, tzinfo=TZ),
            ),
        ]
        ranges = session_hour_ranges_for_day(sessions, day)
        self.assertEqual(ranges[0], [0.0, 1.0])
        self.assertEqual(ranges[1][0], 9.25)
        self.assertEqual(ranges[1][1], 12.0)
        self.assertEqual(ranges[2][0], 13.5)
        self.assertAlmostEqual(ranges[2][1], 17.75)

    def test_overnight_session_starts_at_midnight_on_today_label(self):
        """Cross-midnight presence on the Today tab must label from 00:00, not yesterday 14:xx."""
        day = datetime(2026, 9, 18, tzinfo=TZ)
        sessions = [
            (
                datetime(2026, 9, 17, 14, 2, tzinfo=TZ),
                datetime(2026, 9, 18, 12, 43, tzinfo=TZ),
            ),
        ]
        ranges = session_hour_ranges_for_day(sessions, day)
        self.assertEqual(len(ranges), 1)
        self.assertEqual(ranges[0][0], 0.0)
        self.assertAlmostEqual(ranges[0][1], 12 + 43 / 60.0, places=2)

    def test_parse_at_delimited_devices(self):
        raw = "@2@00:11:22:33:44:55@iPhone@192.168.254.50@wireless@36@80@Allow@aa:bb:cc:dd:ee:ff@HP-Printer@192.168.254.20@wired"
        devices = parse_at_delimited_devices(raw)
        macs = {d["mac"]: d for d in devices}
        self.assertEqual(macs["00:11:22:33:44:55"]["hostname"], "iPhone")
        self.assertEqual(macs["00:11:22:33:44:55"]["ip"], "192.168.254.50")
        self.assertEqual(macs["aa:bb:cc:dd:ee:ff"]["hostname"], "HP-Printer")

    def test_parse_semicolon_r6700_format(self):
        from py.utilities.attendance_netgear import parse_semicolon_attached_devices

        raw = "35@1;192.168.254.49;BAMBU;aa:bb:cc:dd:ee:01;wireless;150;47;Allow@2;192.168.254.29;IPHONE;aa:bb:cc:dd:ee:02;wireless;780;57;Allow"
        devices = {d["mac"]: d for d in parse_semicolon_attached_devices(raw)}
        self.assertEqual(devices["aa:bb:cc:dd:ee:01"]["hostname"], "BAMBU")
        self.assertEqual(devices["aa:bb:cc:dd:ee:02"]["hostname"], "IPHONE")
        self.assertEqual(devices["aa:bb:cc:dd:ee:02"]["ip"], "192.168.254.29")

    def test_parse_device_xml_blocks_ignores_truncated_tail(self):
        from py.utilities.attendance_netgear import parse_device_xml_blocks, parse_netgear_device_xml

        raw = """
        <Device><IP>192.168.254.10</IP><Name>HELEN-PC</Name><MAC>AA:BB:CC:DD:EE:03</MAC><ConnectionType>wired</ConnectionType></Device>
        <Device><IP>192.168.254.4</IP><Name>ISLERO</Name><MAC>AA:BB:CC:DD:EE:04</MAC><SignalStrength</NewAttachDevice>
        """
        devices = parse_device_xml_blocks(raw)
        self.assertEqual(len(devices), 1)
        self.assertEqual(devices[0]["hostname"], "HELEN-PC")
        merged = parse_netgear_device_xml(raw + "35@1;192.168.254.29;IPHONE;aa:bb:cc:dd:ee:05;wireless;780;57;Allow")
        names = {d["hostname"] for d in merged}
        self.assertIn("HELEN-PC", names)
        self.assertIn("IPHONE", names)
        xml = """
        <NewAttachDevice2>
          <Device>
            <MAC>AA-BB-CC-DD-EE-FF</MAC>
            <Name>Galaxy-S24</Name>
            <IPAddress>192.168.254.88</IPAddress>
            <ConnectionType>5GHz</ConnectionType>
          </Device>
        </NewAttachDevice2>
        """
        devices = parse_netgear_device_xml(xml)
        self.assertEqual(len(devices), 1)
        self.assertEqual(devices[0]["mac"], "aa:bb:cc:dd:ee:ff")
        self.assertEqual(devices[0]["hostname"], "Galaxy-S24")
        self.assertEqual(devices[0]["ip"], "192.168.254.88")

    def test_parse_arp_office_subnet(self):
        text = """
Interface: 192.168.254.30 --- 0x3
  Internet Address      Physical Address      Type
  192.168.254.1         9c-3d-cf-c3-0e-02     dynamic
  192.168.1.1           00-11-22-33-44-55     dynamic
  192.168.254.50        ff-ff-ff-ff-ff-ff     static
        """
        devices = parse_arp_table(text)
        self.assertEqual(len(devices), 1)
        self.assertEqual(devices[0]["mac"], "9c:3d:cf:c3:0e:02")
        self.assertEqual(devices[0]["ip"], "192.168.254.1")

    def test_merge_prefers_first_hostname(self):
        merged = merge_device_lists(
            [{"mac": "aa:bb:cc:dd:ee:ff", "hostname": "iPhone", "ip": "192.168.254.9", "source": "netgear"}],
            [{"mac": "AA-BB-CC-DD-EE-FF", "hostname": "", "ip": "192.168.254.9", "source": "arp"}],
        )
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["hostname"], "iPhone")
        self.assertEqual(merged[0]["source"], "netgear")

    def test_enrich_attached_from_arp_does_not_add_arp_only(self):
        from py.utilities.attendance_netgear import enrich_attached_from_arp

        soap = [
            {"mac": "aa:bb:cc:dd:ee:01", "hostname": "Tristen-iPhone", "ip": "192.168.254.29"},
        ]
        arp = [
            {"mac": "aa:bb:cc:dd:ee:01", "hostname": "", "ip": "192.168.254.29"},
            {"mac": "aa:bb:cc:dd:ee:99", "hostname": "stale-phone", "ip": "192.168.254.99"},
        ]
        devices = enrich_attached_from_arp(soap, arp)
        self.assertEqual(len(devices), 1)
        self.assertEqual(devices[0]["mac"], "aa:bb:cc:dd:ee:01")
        self.assertEqual(devices[0]["source"], "netgear")
        self.assertEqual(devices[0]["hostname"], "Tristen-iPhone")

    def test_presence_timing_defaults_are_tight(self):
        from py.utilities import attendance_store as store

        self.assertEqual(store.DEFAULT_POLL_SECONDS, 30)
        self.assertEqual(store.DEFAULT_ABSENT_MINUTES, 4)
        self.assertEqual(store.DEFAULT_SLACK_LEAVE_GRACE_MINUTES, 4)
        self.assertEqual(store.DEFAULT_DNS_PRESENCE_MINUTES, 3)

    def test_slack_leave_grace_helpers(self):
        from py.utilities.attendance_store import (
            normalize_slack_leave_grace_minutes,
            slack_join_ready,
            slack_leave_ready,
        )

        self.assertEqual(normalize_slack_leave_grace_minutes(None), 4)
        self.assertEqual(normalize_slack_leave_grace_minutes(""), 4)
        self.assertEqual(normalize_slack_leave_grace_minutes(10), 10)
        self.assertEqual(normalize_slack_leave_grace_minutes(999), 120)
        self.assertEqual(normalize_slack_leave_grace_minutes(-3), 0)
        self.assertTrue(slack_join_ready(slack_present=False, hidden=False, is_phone=True))
        self.assertFalse(slack_join_ready(slack_present=True, hidden=False, is_phone=True))
        self.assertFalse(slack_join_ready(slack_present=False, hidden=True, is_phone=True))
        now = datetime(2026, 9, 21, 18, 0)
        still_within = datetime(2026, 9, 21, 17, 57)
        self.assertFalse(
            slack_leave_ready(
                slack_present=True,
                hidden=False,
                is_phone=True,
                last_seen_utc=still_within,
                now_utc=now,
                grace_minutes=4,
            )
        )
        past_grace = datetime(2026, 9, 21, 17, 55)
        self.assertTrue(
            slack_leave_ready(
                slack_present=True,
                hidden=False,
                is_phone=True,
                last_seen_utc=past_grace,
                now_utc=now,
                grace_minutes=4,
            )
        )
        self.assertTrue(
            slack_leave_ready(
                slack_present=True,
                hidden=False,
                is_phone=True,
                last_seen_utc=still_within,
                now_utc=now,
                grace_minutes=0,
            )
        )

    def test_dns_debug_name_and_packet_parse(self):
        from py.utilities.attendance_dns import decode_dns_logged_name, parse_dns_debug_lines

        self.assertEqual(decode_dns_logged_name("(3)www(6)google(3)com(0)"), "www.google.com")
        self.assertEqual(
            decode_dns_logged_name("(2)26(3)254(3)168(3)192(7)in-addr(4)arpa(0)"),
            "26.254.168.192.in-addr.arpa",
        )
        sample = [
            "9/17/2026 11:43:05 PM 0FC4 PACKET  00000000014479A0 UDP Rcv 192.168.254.10  ab12   Q [0001   D   NOERROR] A      (3)www(7)example(3)com(0)",
            "9/17/2026 11:43:06 PM 0FC4 PACKET  00000000014479A1 UDP Snd 8.8.8.8         ab12   Q [0001   D   NOERROR] A      (3)www(7)example(3)com(0)",
            "9/17/2026 11:43:07 PM 0FC4 PACKET  00000000014479A2 UDP Rcv 192.168.254.10  cd",
            "34   Q [0001   D   NOERROR] AAAA   (6)github(3)com(0)",
        ]
        rows = parse_dns_debug_lines(sample)
        names = {r["qname"] for r in rows}
        self.assertIn("www.example.com", names)
        self.assertIn("github.com", names)
        self.assertTrue(all(r["client_ip"] == "192.168.254.10" for r in rows))
        self.assertEqual(len(rows), 2)

    def test_dns_presence_settings_defaults(self):
        from py.utilities import attendance_store as store

        with mock.patch.object(
            store,
            "_netgear_creds",
            return_value={"host": "h", "user": "u", "password": "p"},
        ):
            with mock.patch.object(store, "ensure_schema"):
                with mock.patch.object(store, "_conn", return_value=None):
                    with mock.patch.dict(
                        "os.environ",
                        {
                            "ATTENDANCE_DNS_PRESENCE": "1",
                            "ATTENDANCE_DNS_PRESENCE_MINUTES": "3",
                        },
                        clear=False,
                    ):
                        s = store.get_settings()
                    with mock.patch.dict(
                        "os.environ",
                        {"ATTENDANCE_DNS_PRESENCE": "0"},
                        clear=False,
                    ):
                        s_off = store.get_settings()
        self.assertTrue(s["dns_presence_enabled"])
        self.assertEqual(s["dns_presence_minutes"], 3)
        self.assertFalse(s_off["dns_presence_enabled"])

    def test_ingest_dns_log_if_stale_skips_fresh(self):
        from py.utilities import attendance_dns as dns

        fresh_local = datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S")
        with mock.patch.object(dns, "ensure_dns_schema"):
            with mock.patch.object(dns, "_get_meta", return_value=fresh_local):
                with mock.patch.object(dns, "ingest_dns_log") as ingest:
                    result = dns.ingest_dns_log_if_stale(max_age_seconds=40)
        self.assertTrue(result.get("skipped"))
        ingest.assert_not_called()

    def test_guess_device_type(self):
        self.assertEqual(guess_device_type("Tristen-iPhone"), "phone")
        self.assertEqual(guess_device_type("HP-LaserJet-M428"), "printer")
        self.assertEqual(guess_device_type("DESKTOP-OFFICE"), "computer")
        self.assertEqual(guess_device_type("Deco-X75"), "other")

    def test_phone_notify_candidate(self):
        self.assertTrue(is_phone_notify_candidate("phone", "unknown", "", ""))
        self.assertTrue(is_phone_notify_candidate("", "phone", "", ""))
        self.assertTrue(is_phone_notify_candidate("", "", "iPhone-de-Ting", ""))
        self.assertFalse(is_phone_notify_candidate("computer", "warehouse_service", "ISLERO", "Ken PC"))
        self.assertFalse(is_phone_notify_candidate("phone", "unknown", "WATCH", "Helen iWatch"))

    def test_night_window_and_bucket(self):
        evening = datetime(2026, 9, 17, 22, 30, tzinfo=TZ)
        morning = datetime(2026, 9, 18, 5, 15, tzinfo=TZ)
        noon = datetime(2026, 9, 18, 12, 0, tzinfo=TZ)
        self.assertTrue(is_night(evening, "22:00", "06:00"))
        self.assertTrue(is_night(morning, "22:00", "06:00"))
        self.assertFalse(is_night(noon, "22:00", "06:00"))
        self.assertEqual(night_bucket_for(evening, "22:00"), "2026-09-17")
        self.assertEqual(night_bucket_for(morning, "22:00"), "2026-09-17")

    def test_classify_phones_vs_warehouse(self):
        self.assertEqual(classify_auto_role(0, 3), "phone")
        self.assertEqual(classify_auto_role(5, 0), "warehouse_service")
        self.assertEqual(classify_auto_role(1, 0), "unknown")
        self.assertEqual(classify_auto_role(2, 2), "unknown")

    def test_session_overlap(self):
        start = datetime(2026, 9, 17, 8, 0)
        end = datetime(2026, 9, 17, 12, 0)
        window_start = datetime(2026, 9, 17, 0, 0)
        window_end = datetime(2026, 9, 18, 0, 0)
        self.assertEqual(session_overlap_seconds(start, end, window_start, window_end), 4 * 3600)

    def test_chart_payload_ranges_use_clock_hours(self):
        from py.utilities import attendance_store as store

        sessions = [
            {
                "started": "2026-09-17 09:30:00",
                "ended": "2026-09-17 10:00:00",
                "open": False,
            }
        ]
        with mock.patch.object(store, "list_sessions", return_value=sessions):
            payload = store.chart_payload(
                "aa:bb:cc:dd:ee:ff",
                month="",
                start="2026-09-17",
                end="2026-09-17",
            )
        self.assertEqual(payload["labels"], ["09/17"])
        self.assertEqual(payload["hours"], [0.5])
        self.assertEqual(payload["ranges"], [[[9.5, 10.0]]])
        self.assertEqual(payload["total_hours"], 0.5)

    def test_soap_login_v1_body_matches_genie(self):
        from py.utilities.attendance_netgear import soap_login_v1_body

        body = soap_login_v1_body("admin", "p&ss<>")
        self.assertIn("<Authenticate>", body)
        self.assertNotIn("xmlns=", body.split("<Authenticate>", 1)[1].split("</Authenticate>", 1)[0])
        self.assertIn("<NewUsername>admin</NewUsername>", body)
        self.assertIn("<NewPassword>p&amp;ss&lt;&gt;</NewPassword>", body)

    def test_env_file_values(self):
        from py.utilities import attendance_store as store

        sample = "NETGEAR_HOST=192.168.254.1\nNETGEAR_USER=admin\nNETGEAR_PASSWORD=secret\n"
        with mock.patch.object(store, "env_file_path") as path_fn:
            path = mock.Mock()
            path.is_file.return_value = True
            path.read_text.return_value = sample
            path_fn.return_value = path
            vals = store.parse_env_file_values(("NETGEAR_HOST", "NETGEAR_USER", "NETGEAR_PASSWORD"))
        self.assertEqual(vals["NETGEAR_HOST"], "192.168.254.1")
        self.assertEqual(vals["NETGEAR_USER"], "admin")
        self.assertEqual(vals["NETGEAR_PASSWORD"], "secret")

    def test_yeti_elaine_only(self):
        self.assertEqual(TOOL_BY_BLUEPRINT["attendance_log"], "attendance_log")
        self.assertTrue(any(t["id"] == "attendance_log" for t in DASHBOARD_TOOLS))
        self.assertEqual(DASHBOARD_TOOL_NAV["attendance_log"]["href"], "/attendance_log")
        self.assertEqual(ATTENDANCE_LOG_USERS, frozenset({"yeti", "elaine"}))
        self.assertTrue(tool_visible_for_user({}, "yeti", "attendance_log"))
        self.assertTrue(tool_visible_for_user({"tools": []}, "elaine", "attendance_log"))
        self.assertFalse(tool_visible_for_user({}, "rose", "attendance_log"))
        self.assertFalse(tool_visible_for_user({"tools": ["attendance_log"]}, "rose", "attendance_log"))

    def test_locales_and_module_wired(self):
        from py.utilities.module_loader import OPTIONAL_MODULES

        en = json.loads((ROOT / "locales" / "en.json").read_text(encoding="utf-8"))
        zh = json.loads((ROOT / "locales" / "zh.json").read_text(encoding="utf-8"))
        self.assertEqual(set(en), set(zh))
        self.assertTrue(en["tool.attendance_log.title"])
        self.assertTrue(en["page.attendance_log.title"])
        self.assertTrue(zh["tool.attendance_log.title"])
        names = {m["name"] for m in OPTIONAL_MODULES}
        self.assertIn("attendance_log", names)

    def test_degraded_poll_does_not_close_sessions(self):
        from py.utilities import attendance_store as store

        captured = {}

        def fake_apply(devices, **kwargs):
            captured["complete"] = kwargs.get("complete")
            captured["source"] = kwargs.get("source")
            captured["count"] = len(devices)
            return {"ok": True, **captured}

        with mock.patch.object(store, "get_settings", return_value={
            "router_host": "192.168.254.1",
            "router_user": "admin",
            "router_password": "secret",
            "password_set": True,
            "subnet": "192.168.254.0/24",
        }):
            with mock.patch.object(store, "known_ips", return_value=[]):
                with mock.patch("py.utilities.attendance_store.fetch_office_devices", return_value={
                    "ok": False,
                    "complete": False,
                    "source": "arp",
                    "error": "SOAP ResponseCode 401",
                    "devices": [{"mac": "aa:bb:cc:dd:ee:ff", "ip": "192.168.254.9"}],
                    "url": "",
                }):
                    with mock.patch.object(store, "apply_snapshot", side_effect=fake_apply):
                        store.run_poll(unpooled=False)
        self.assertFalse(captured["complete"])
        self.assertEqual(captured["source"], "arp")


if __name__ == "__main__":
    unittest.main()
