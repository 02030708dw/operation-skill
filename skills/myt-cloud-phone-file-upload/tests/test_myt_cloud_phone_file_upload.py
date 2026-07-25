from __future__ import annotations

import importlib.util
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sys
import tempfile
import threading
import unittest


SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "myt_cloud_phone_file_upload.py"
)
SPEC = importlib.util.spec_from_file_location("myt_file_upload", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class UploadHelpersTest(unittest.TestCase):
    def test_device_mapping_and_deduplication(self) -> None:
        devices = MODULE.parse_devices("T1001,T1002,10005", 10005, 3)
        self.assertEqual(
            [(item.label, item.port) for item in devices],
            [("T1001", 10005), ("T1002", 10008)],
        )

    def test_path_is_required(self) -> None:
        with self.assertRaises(MODULE.ConfigurationError):
            MODULE.validate_input_path("")

    def test_single_file_accepts_non_video_extension(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "data.json"
            path.write_text("{}", encoding="utf-8")
            validated = MODULE.validate_input_path(str(path))
            entries = MODULE.collect_input_files(validated, None)
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0].relative_remote_path, "data.json")

    def test_directory_collects_mp4_png_and_nested_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "2026-07-24-result.mp4").write_bytes(b"video")
            (root / "2026-07-24-result.png").write_bytes(b"image")
            (root / "metadata").mkdir()
            (root / "metadata" / "result.json").write_text(
                "{}",
                encoding="utf-8",
            )
            entries = MODULE.collect_input_files(root, None)
            self.assertEqual(
                [item.relative_remote_path for item in entries],
                [
                    "2026-07-24-result.mp4",
                    "2026-07-24-result.png",
                    "metadata/result.json",
                ],
            )

    def test_directory_rejects_remote_name(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(MODULE.ConfigurationError):
                MODULE.collect_input_files(Path(directory), "renamed.bin")

    def test_non_ascii_names_get_stable_unique_ascii_names(self) -> None:
        first = MODULE.portable_remote_component("开奖结果.png")
        second = MODULE.portable_remote_component("开奖时间.png")
        self.assertRegex(first, r"^file-[0-9a-f]{8}\.png$")
        self.assertNotEqual(first, second)

    def test_remote_dir_rejects_parent_traversal(self) -> None:
        with self.assertRaises(MODULE.ConfigurationError):
            MODULE.validate_remote_dir("/sdcard/../data")

    def test_default_landing_directory_matches_real_device(self) -> None:
        self.assertEqual(MODULE.UPLOAD_STAGING_DIR, "/sdcard/upload")
        self.assertEqual(MODULE.DEFAULT_REMOTE_DIR, "/sdcard/upload")

    def test_streaming_multipart_upload_uses_file_field(self) -> None:
        received: dict[str, bytes | str] = {}

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers["Content-Length"])
                received["path"] = self.path
                received["content_type"] = self.headers["Content-Type"]
                received["body"] = self.rfile.read(length)
                self.send_response(200)
                self.end_headers()
                self.wfile.write("文件上传完成！".encode("utf-8"))

            def log_message(self, *_args: object) -> None:
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "sample.png"
                path.write_bytes(b"image-payload")
                device = MODULE.Device("test", server.server_port)
                client = MODULE.MytClient(
                    "127.0.0.1",
                    device,
                    request_timeout=5,
                    verbose=False,
                )
                response = client.upload(
                    path,
                    "hermes-temporary.upload",
                    progress_step=100,
                )
            self.assertEqual(response, "文件上传完成！")
            self.assertEqual(received["path"], "/upload")
            self.assertIn("multipart/form-data", str(received["content_type"]))
            body = bytes(received["body"])
            self.assertIn(b'name="file"', body)
            self.assertIn(b'filename="hermes-temporary.upload"', body)
            self.assertIn(b"image-payload", body)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
