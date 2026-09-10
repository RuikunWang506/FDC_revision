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


if __name__ == "__main__":
    unittest.main()
