import importlib.util
from pathlib import Path
import tempfile
import types
import unittest
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "download.py"
SPEC = importlib.util.spec_from_file_location("youtube_downloader", SCRIPT)
DOWNLOADER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(DOWNLOADER)


class ServerRuntimeTests(unittest.TestCase):
    def test_system_ffmpeg_needs_no_generated_tool_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            executable = root / "ffmpeg"
            executable.touch()
            output = root / "videos"
            output.mkdir()
            with mock.patch.object(DOWNLOADER.shutil, "which", return_value=str(executable)):
                self.assertEqual(str(executable.resolve()), DOWNLOADER.resolve_ffmpeg(output))
            self.assertFalse((output / ".tools").exists())

    def test_fallback_lives_in_output_and_tracks_runtime_upgrade(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old = root / "ffmpeg-v1"
            new = root / "ffmpeg-v2"
            old.touch()
            new.touch()
            bundled = types.SimpleNamespace(get_ffmpeg_exe=lambda: str(old))
            with mock.patch.object(DOWNLOADER.shutil, "which", return_value=None), mock.patch.dict("sys.modules", {"imageio_ffmpeg": bundled}):
                alias = Path(DOWNLOADER.resolve_ffmpeg(root))
                self.assertEqual(root / ".tools/ffmpeg", alias)
                self.assertEqual(old.resolve(), alias.resolve())
                bundled.get_ffmpeg_exe = lambda: str(new)
                self.assertEqual(alias, Path(DOWNLOADER.resolve_ffmpeg(root)))
                self.assertEqual(new.resolve(), alias.resolve())

    def test_explicit_missing_ffmpeg_is_actionable(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, "FFmpeg"):
                DOWNLOADER.resolve_ffmpeg(Path(tmp), str(Path(tmp) / "missing"))

    def test_channel_and_single_link_scope(self):
        self.assertEqual(("https://www.youtube.com/@Wimbledon/shorts", None), DOWNLOADER.normalize_url("https://www.youtube.com/@Wimbledon"))
        self.assertEqual(("https://www.youtube.com/watch?v=0ixRDhyAsMY", "0ixRDhyAsMY"), DOWNLOADER.normalize_url("https://www.youtube.com/watch?v=0ixRDhyAsMY&list=another-list"))
        with self.assertRaises(ValueError):
            DOWNLOADER.normalize_url("https://youtube.com.example.test/shorts/0ixRDhyAsMY")


if __name__ == "__main__":
    unittest.main()
