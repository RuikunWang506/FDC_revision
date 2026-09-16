import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from py.other.bulletin import (
    create_post,
    delete_post,
    list_visible_posts,
    preview_posts,
    resolve_attachment,
    sanitize_html,
    serialize_post,
)


class FakeUpload:
    def __init__(self, filename, data):
        self.filename = filename
        self._data = data

    def read(self):
        return self._data


class BulletinStorageTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_sanitize_strips_script_and_bad_images(self):
        html = sanitize_html(
            '<p>Hi<script>alert(1)</script></p>'
            '<img src="javascript:alert(1)">'
            '<img src="/bulletin/files/abc/photo.png">'
            '<span style="color:red; background:url(javascript:alert(1))">x</span>'
        )
        self.assertIn("Hi", html)
        self.assertNotIn("script", html.lower())
        self.assertNotIn("javascript", html.lower())
        self.assertNotIn("url(", html.lower())
        self.assertIn("/bulletin/files/abc/photo.png", html)
        self.assertIn("color: red", html)

    def test_private_posts_are_limited_to_author_and_recipients(self):
        create_post("alice", "<p>secret sheet</p>", "private", "bob", [], root=self.root)
        self.assertEqual(len(list_visible_posts("alice", "private", root=self.root)), 1)
        self.assertEqual(len(list_visible_posts("bob", "private", root=self.root)), 1)
        self.assertEqual(len(list_visible_posts("carol", "private", root=self.root)), 0)
        self.assertEqual(len(list_visible_posts("carol", "public", root=self.root)), 0)

    def test_preview_returns_newest_two_public_posts(self):
        create_post("alice", "<p>one</p>", "public", "", [], root=self.root)
        create_post("alice", "<p>two</p>", "public", "", [], root=self.root)
        create_post("alice", "<p>three</p>", "public", "", [], root=self.root)
        create_post("alice", "<p>hidden</p>", "private", "", [], root=self.root)
        preview = preview_posts("bob", root=self.root)
        self.assertEqual(len(preview), 2)
        self.assertEqual(preview[0]["text_preview"], "three")
        self.assertEqual(preview[1]["text_preview"], "two")

    def test_file_upload_and_blocked_extension(self):
        post = create_post(
            "alice",
            "<p>drop</p>",
            "public",
            "",
            [
                FakeUpload("quote.xlsx", b"excel-bytes"),
                FakeUpload("virus.exe", b"MZ"),
            ],
            root=self.root,
        )
        names = [att["original_name"] for att in post["attachments"]]
        self.assertEqual(names, ["quote.xlsx"])
        stored = post["attachments"][0]["stored_name"]
        found = resolve_attachment(post["id"], stored, "bob", root=self.root)
        self.assertIsNotNone(found)
        self.assertEqual(found["path"].read_bytes(), b"excel-bytes")
        self.assertIsNone(resolve_attachment(post["id"], "../secret.txt", "bob", root=self.root))
        self.assertIsNone(resolve_attachment("not-a-real-id", stored, "bob", root=self.root))

    def test_private_file_is_not_downloadable_by_outsiders(self):
        post = create_post(
            "alice",
            "<p></p>",
            "private",
            "",
            [FakeUpload("only-me.png", b"\x89PNG")],
            root=self.root,
        )
        stored = post["attachments"][0]["stored_name"]
        self.assertIsNotNone(resolve_attachment(post["id"], stored, "alice", root=self.root))
        self.assertIsNone(resolve_attachment(post["id"], stored, "bob", root=self.root))

    def test_author_can_delete(self):
        post = create_post("alice", "<p>bye</p>", "public", "", [], root=self.root)
        self.assertTrue(delete_post(post["id"], "alice", root=self.root))
        self.assertEqual(list_visible_posts("alice", "public", root=self.root), [])

    def test_serialize_includes_download_url(self):
        post = create_post(
            "alice",
            "<b>hi</b>",
            "public",
            "",
            [FakeUpload("pack.zip", b"PK")],
            root=self.root,
        )
        payload = serialize_post(post, "bob")
        self.assertTrue(payload["attachments"][0]["url"].startswith("/bulletin/files/"))
        self.assertFalse(payload["can_delete"])


if __name__ == "__main__":
    unittest.main()
