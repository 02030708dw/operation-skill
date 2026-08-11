import importlib.util
import json
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
    def test_daily_recent_video_target_defaults_to_ten(self):
        self.assertEqual(MODULE.DEFAULT_DAILY_COUNT, 10)

    def test_browser_profile_is_disabled_until_login_marker_exists(self):
        with tempfile.TemporaryDirectory() as temporary:
            args = MODULE.build_parser().parse_args(
                ["--browser-profile", temporary, "--check"]
            )
            self.assertIsNone(MODULE.enabled_browser_profile(args))
            (Path(temporary) / MODULE.LOGIN_MARKER).write_text(
                "authorized\n", encoding="utf-8"
            )
            self.assertEqual(
                MODULE.enabled_browser_profile(args),
                Path(temporary).resolve(),
            )

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

    def test_single_source_file_does_not_modify_persistent_configuration(self):
        with tempfile.TemporaryDirectory() as temporary:
            accounts = Path(temporary) / "accounts.txt"
            MODULE._write_single_source(
                accounts,
                ["creator", "https://www.facebook.com/example/reels/"],
            )
            self.assertEqual(
                MODULE.configured_sources(accounts),
                [("creator", "https://www.facebook.com/example/reels/")],
            )

    def test_result_manifest_gets_execution_and_exit_code(self):
        with tempfile.TemporaryDirectory() as temporary:
            result_path = Path(temporary) / "result.json"
            result_path.write_text(
                json.dumps(
                    {
                        "schemaVersion": "1.0",
                        "skill": MODULE.SKILL_NAME,
                        "status": "completed",
                        "sources": [],
                    }
                ),
                encoding="utf-8",
            )
            payload = MODULE._load_result(result_path, 0, "E-001")
            self.assertEqual(payload["executionId"], "E-001")
            self.assertEqual(payload["exitCode"], 0)


if __name__ == "__main__":
    unittest.main()
