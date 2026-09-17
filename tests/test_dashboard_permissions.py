import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "fdc_dashboard_permissions", ROOT / "py/utilities/dashboard_permissions.py"
)
perms = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(perms)


class DashboardPermissionTests(unittest.TestCase):
    def test_yeti_sees_all_tools(self):
        self.assertTrue(perms.tool_visible_for_user({"tools": []}, "yeti", "order_lookup"))

    def test_restricted_group_only_listed_tools(self):
        data = {"tools": ["clientview", "master_image_viewer"]}
        self.assertTrue(perms.tool_visible_for_user(data, "xingxing", "clientview"))
        self.assertFalse(perms.tool_visible_for_user(data, "xingxing", "manage_users"))

    def test_unrestricted_when_tools_omitted(self):
        self.assertTrue(perms.tool_visible_for_user({}, "rose", "item_lookup"))

    def test_delete_user_mapped_to_manage_users_acl(self):
        self.assertEqual(perms.ENDPOINT_TOOL["auth.delete_user"], "manage_users")
        self.assertEqual(perms.ENDPOINT_TOOL["auth.edit_user"], "manage_users")

    def test_bulletin_is_a_dashboard_tool(self):
        self.assertEqual(perms.TOOL_BY_BLUEPRINT["bulletin"], "bulletin")
        self.assertTrue(any(t["id"] == "bulletin" for t in perms.DASHBOARD_TOOLS))
        self.assertEqual(perms.DASHBOARD_TOOL_NAV["bulletin"]["href"], "/bulletin")

    def test_bulletin_default_groups_and_explicit_tools(self):
        self.assertTrue(perms.bulletin_visible_for_group({}, "rose", "manager"))
        self.assertTrue(perms.bulletin_visible_for_group({}, "xj", "superadmin"))
        self.assertTrue(perms.bulletin_visible_for_group({}, "daniel", "warehouse"))
        self.assertTrue(perms.bulletin_visible_for_group({"tools": []}, "yeti", "visitor"))
        self.assertFalse(perms.bulletin_visible_for_group({}, "kenny", "visitor"))
        self.assertFalse(perms.bulletin_visible_for_group({}, "adminuser", "admin"))
        self.assertFalse(
            perms.bulletin_visible_for_group({"tools": ["item_lookup"]}, "rose", "manager")
        )
        self.assertTrue(
            perms.tool_visible_for_user({"tools": ["item_lookup", "bulletin"]}, "kenny", "bulletin")
        )
        self.assertFalse(perms.tool_visible_for_user({"tools": ["item_lookup"]}, "rose", "bulletin"))
        self.assertTrue(perms.tool_visible_for_user({"tools": []}, "yeti", "bulletin"))

    def test_location_service_replaces_legacy_cards(self):
        ids = {t["id"] for t in perms.DASHBOARD_TOOLS}
        self.assertIn("location_service", ids)
        self.assertNotIn("burlington_highlight", ids)
        self.assertNotIn("external_stock_check", ids)
        self.assertNotIn("internal_stock_check", ids)
        self.assertNotIn("oosc", ids)
        self.assertNotIn("location_massive_update", ids)
        self.assertNotIn("warehousemap", ids)
        self.assertEqual(perms.DASHBOARD_TOOL_NAV["location_service"]["href"], "/locations")

    def test_legacy_location_permissions_open_hub(self):
        self.assertTrue(
            perms.tool_visible_for_user({"tools": ["warehousemap"]}, "rose", "location_service")
        )
        self.assertTrue(
            perms.tool_visible_for_user(
                {"tools": ["location_massive_update"]}, "rose", "warehousemap"
            )
        )
        self.assertTrue(
            perms.tool_visible_for_user({"tools": ["location_service"]}, "rose", "oosc")
        )
        self.assertFalse(
            perms.tool_visible_for_user({"tools": ["item_lookup"]}, "rose", "location_service")
        )

    def test_tools_sel_for_ui_maps_legacy_location_ids(self):
        self.assertIn(
            "location_service",
            perms.tools_sel_for_ui(["warehousemap", "item_lookup"]),
        )
        self.assertEqual(
            perms.tools_sel_for_ui(["location_service", "item_lookup"]).count("location_service"),
            1,
        )


if __name__ == "__main__":
    unittest.main()
