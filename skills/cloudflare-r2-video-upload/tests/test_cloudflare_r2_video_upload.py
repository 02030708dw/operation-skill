import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path


MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "cloudflare_r2_video_upload.py"
)
SPEC = importlib.util.spec_from_file_location("cloudflare_r2_video_upload", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class DiscoveryTests(unittest.TestCase):
    def test_discovers_videos_and_preserves_relative_paths(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "nested").mkdir()
            (root / "one.mp4").write_bytes(b"one")
            (root / "nested" / "two.MOV").write_bytes(b"two")
            (root / "ignore.txt").write_text("ignore", encoding="utf-8")
            items = MODULE.discover_files(
                root,
                "facebook/2026",
                all_files=False,
                include_hidden=False,
                flatten=False,
                count=0,
            )
            self.assertEqual(
                [item.key for item in items],
                ["facebook/2026/nested/two.MOV", "facebook/2026/one.mp4"],
            )

    def test_hidden_files_are_skipped(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / ".hidden.mp4").write_bytes(b"hidden")
            (root / "visible.mp4").write_bytes(b"visible")
            items = MODULE.discover_files(
                root,
                "",
                all_files=False,
                include_hidden=False,
                flatten=False,
                count=0,
            )
            self.assertEqual([item.key for item in items], ["visible.mp4"])

    def test_flatten_collision_stops(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "a").mkdir()
            (root / "b").mkdir()
            (root / "a" / "same.mp4").write_bytes(b"a")
            (root / "b" / "same.mp4").write_bytes(b"b")
            with self.assertRaises(ValueError):
                MODULE.discover_files(
                    root,
                    "",
                    all_files=False,
                    include_hidden=False,
                    flatten=True,
                    count=0,
                )

    def test_public_url_encodes_unicode_and_spaces(self):
        url = MODULE.public_url("https://media.example.com", "视频/a b.mp4")
        self.assertEqual(
            url,
            "https://media.example.com/%E8%A7%86%E9%A2%91/a%20b.mp4",
        )

    def test_multipart_chunk_must_be_at_least_five_mib(self):
        self.assertEqual(MODULE.multipart_chunk_mib("5"), 5)
        with self.assertRaises(Exception):
            MODULE.multipart_chunk_mib("4")


if __name__ == "__main__":
    unittest.main()
