import importlib.util
import tempfile
import unittest
from pathlib import Path


MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "facebook_followed_video_download.py"
)
SPEC = importlib.util.spec_from_file_location("facebook_followed_video_download", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MODULE)


class EntryPointTests(unittest.TestCase):
    def test_accepts_facebook_urls_only(self):
        self.assertTrue(MODULE.is_facebook_url("https://www.facebook.com/example/reels/"))
        self.assertTrue(MODULE.is_facebook_url("https://fb.watch/example/"))
        self.assertFalse(MODULE.is_facebook_url("https://example.com/video"))

    def test_source_file_requires_tab_separator(self):
        with tempfile.TemporaryDirectory() as temporary:
            accounts = Path(temporary) / "accounts.txt"
            accounts.write_text(
                "creator-one\thttps://www.facebook.com/example/reels/\n",
                encoding="utf-8",
            )
            self.assertEqual(
                MODULE.configured_sources(accounts),
                [("creator-one", "https://www.facebook.com/example/reels/")],
            )
            accounts.write_text(
                "creator-one https://www.facebook.com/example/reels/\n",
                encoding="utf-8",
            )
            with self.assertRaises(ValueError):
                MODULE.configured_sources(accounts)

    def test_add_source_replaces_same_folder(self):
        with tempfile.TemporaryDirectory() as temporary:
            accounts = Path(temporary) / "accounts.txt"
            MODULE.add_source(accounts, "creator", "https://www.facebook.com/old/reels/")
            MODULE.add_source(accounts, "creator", "https://www.facebook.com/new/reels/")
            self.assertEqual(
                MODULE.configured_sources(accounts),
                [("creator", "https://www.facebook.com/new/reels/")],
            )


if __name__ == "__main__":
    unittest.main()
