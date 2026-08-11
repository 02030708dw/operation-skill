import importlib.util
import json
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

    def test_manifest_selects_only_downloaded_videos_and_preserves_metadata(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            video = root / "result.mp4"
            video.write_bytes(b"video")
            manifest = root / "download.json"
            manifest.write_text(
                json.dumps(
                    {
                        "sources": [
                            {
                                "name": "creator-one",
                                "url": "https://www.facebook.com/example/reels/",
                                "videos": [
                                    {
                                        "platform": "Facebook",
                                        "platformVideoId": "123",
                                        "originalUrl": "https://www.facebook.com/reel/123",
                                        "canonicalUrl": "https://www.facebook.com/reel/123",
                                        "localPath": str(video),
                                        "fileName": video.name,
                                        "fileSize": video.stat().st_size,
                                        "sha256": MODULE.sha256_file(video),
                                        "status": "downloaded",
                                    },
                                    {
                                        "platformVideoId": "456",
                                        "status": "download-failed",
                                    },
                                ],
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            items = MODULE.discover_manifest(manifest, "facebook", 0)
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0].key, "facebook/creator-one/result.mp4")
            self.assertEqual(items[0].metadata["platformVideoId"], "123")
            self.assertEqual(items[0].metadata["source"], "creator-one")

    def test_manifest_can_flatten_source_into_required_task_prefix(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            video = root / "result.mp4"
            video.write_bytes(b"video")
            manifest = root / "download.json"
            manifest.write_text(
                json.dumps(
                    {
                        "sources": [
                            {
                                "name": "PH Sports Official",
                                "videos": [
                                    {
                                        "localPath": str(video),
                                        "fileName": video.name,
                                        "fileSize": video.stat().st_size,
                                        "status": "downloaded",
                                    }
                                ],
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            items = MODULE.discover_manifest(
                manifest,
                "PH/Sports/202608/10",
                0,
                flatten=True,
            )
            self.assertEqual(
                items[0].key,
                "PH/Sports/202608/10/result.mp4",
            )

    def test_manifest_rejects_size_mismatch(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            video = root / "result.mp4"
            video.write_bytes(b"video")
            manifest = root / "download.json"
            manifest.write_text(
                json.dumps(
                    {
                        "videos": [
                            {
                                "localPath": str(video),
                                "fileSize": 999,
                                "status": "downloaded",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaises(ValueError):
                MODULE.discover_manifest(manifest, "", 0)

    def test_manifest_rejects_sha256_mismatch_even_when_size_matches(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            video = root / "result.mp4"
            video.write_bytes(b"video")
            manifest = root / "download.json"
            manifest.write_text(
                json.dumps(
                    {
                        "videos": [
                            {
                                "localPath": str(video),
                                "fileSize": video.stat().st_size,
                                "sha256": "0" * 64,
                                "status": "downloaded",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaises(ValueError):
                MODULE.discover_manifest(manifest, "", 0)


if __name__ == "__main__":
    unittest.main()
