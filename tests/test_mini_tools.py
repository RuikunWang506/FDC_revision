import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from py.other.mini_tools import (
    CONVERT_EXTENSIONS,
    allowed_convert_name,
    convert_engine,
    convert_status_payload,
    gotenberg_url,
)


class MiniToolsHelperTests(unittest.TestCase):
    def test_allowed_office_extensions(self):
        self.assertTrue(allowed_convert_name("invoice.docx"))
        self.assertTrue(allowed_convert_name("orders.XLSX"))
        self.assertTrue(allowed_convert_name("notes.odt"))
        self.assertFalse(allowed_convert_name("photo.jpg"))
        self.assertFalse(allowed_convert_name("label.pdf"))
        self.assertTrue(".docx" in CONVERT_EXTENSIONS)

    def test_gotenberg_url_strips_slash(self):
        with mock.patch.dict(os.environ, {"GOTENBERG_URL": "http://127.0.0.1:3000/"}, clear=False):
            self.assertEqual(gotenberg_url(), "http://127.0.0.1:3000")

    def test_convert_engine_prefers_gotenberg(self):
        with mock.patch.dict(os.environ, {"GOTENBERG_URL": "http://gotenberg:3000"}, clear=False):
            engine, location = convert_engine()
        self.assertEqual(engine, "gotenberg")
        self.assertEqual(location, "http://gotenberg:3000")

    def test_status_payload_when_unconfigured(self):
        with mock.patch.dict(os.environ, {"GOTENBERG_URL": ""}, clear=False):
            with mock.patch("py.other.mini_tools.find_soffice", return_value=""):
                payload = convert_status_payload()
        self.assertFalse(payload["available"])
        self.assertEqual(payload["engine"], "")

    def test_tool_wired_in_permissions_and_locales(self):
        from py.utilities.dashboard_permissions import (
            DASHBOARD_TOOL_NAV,
            DASHBOARD_TOOLS,
            TOOL_BY_BLUEPRINT,
        )

        self.assertEqual(TOOL_BY_BLUEPRINT["mini_tools"], "mini_tools")
        self.assertTrue(any(t["id"] == "mini_tools" for t in DASHBOARD_TOOLS))
        self.assertEqual(DASHBOARD_TOOL_NAV["mini_tools"]["href"], "/mini_tools")
        en = json.loads((ROOT / "locales" / "en.json").read_text(encoding="utf-8"))
        zh = json.loads((ROOT / "locales" / "zh.json").read_text(encoding="utf-8"))
        self.assertEqual(set(en), set(zh))
        self.assertTrue(en["tool.mini_tools.title"])
        self.assertTrue(en["page.mini_tools.convert_btn"])
        self.assertTrue(zh["tool.mini_tools.title"])

    def test_merge_words_groups_lines(self):
        from py.utilities.mini_tools_ocr import extract_words, merge_words

        text = merge_words(
            [
                {"text": "PASSPORT", "conf": 82, "x": 10, "y": 20},
                {"text": "United", "conf": 94, "x": 10, "y": 200},
                {"text": "States", "conf": 95, "x": 80, "y": 202},
                {"text": "of", "conf": 96, "x": 10, "y": 240},
                {"text": "America", "conf": 96, "x": 40, "y": 241},
            ]
        )
        self.assertEqual(text, "PASSPORT\nUnited States\nof America")
        data = {
            "text": ["PASSPORT", "ry", "|", "United"],
            "conf": ["82", "77", "89", "94"],
            "left": [10, 200, 5, 10],
            "top": [20, 30, 18, 200],
        }
        words = extract_words(data)
        self.assertEqual([w["text"] for w in words], ["PASSPORT", "United"])

    def test_ai_picks_qwen_vl_before_llava(self):
        from py.utilities.mini_tools_ai import AI_TASKS, _VISION_HINTS, _pick_named, _strip_fence

        models = ["llama3.2:latest", "llava:latest", "qwen2.5vl:7b", "dolphin3:8b"]
        self.assertEqual(_pick_named(models, _VISION_HINTS, vision=True), "qwen2.5vl:7b")
        self.assertEqual(_strip_fence("```json\n{\"po\":\"1\"}\n```"), '{"po":"1"}')
        self.assertIn("extract", AI_TASKS)
        self.assertIn("translate_zh", AI_TASKS)


if __name__ == "__main__":
    unittest.main()
