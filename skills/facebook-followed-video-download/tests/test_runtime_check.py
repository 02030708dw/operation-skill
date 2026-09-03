import ctypes
import importlib.util
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "facebook_followed_video_download.py"
)
SPEC = importlib.util.spec_from_file_location("downloader_runtime_check", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MODULE)


class ProbeOutputTests(unittest.TestCase):
    def test_utf8_takes_priority_over_windows_locale(self):
        text = "检查完成：视频 🎬"
        with mock.patch.object(MODULE.locale, "getpreferredencoding", return_value="cp936"):
            self.assertEqual(MODULE._decode_probe_output(text.encode("utf-8")), text)

    def test_windows_gbk_output_preserves_chinese(self):
        text = "正在检查浏览器"
        self.assertEqual(text.encode("gbk")[0], 0xD5)
        with mock.patch.object(MODULE.sys, "platform", "win32"), \
                mock.patch.object(MODULE.locale, "getpreferredencoding", return_value="cp936"):
            self.assertEqual(MODULE._decode_probe_output(text.encode("gbk")), text)

    @unittest.skipUnless(sys.platform == "win32", "requires native Windows ANSI codec")
    def test_windows_native_encoding_is_used_even_in_python_utf8_mode(self):
        text = "caf\u00e9"
        output = text.encode("mbcs")
        with mock.patch.object(MODULE.locale, "getpreferredencoding", return_value="utf-8"):
            self.assertEqual(MODULE._decode_probe_output(output), text)

    def test_unrecognized_bytes_and_codec_do_not_raise(self):
        with mock.patch.object(MODULE.sys, "platform", "linux"), \
                mock.patch.object(MODULE.locale, "getpreferredencoding", return_value="missing-codec"):
            self.assertEqual(MODULE._decode_probe_output(b"\xd5\xff"), "\ufffd\ufffd")
            self.assertEqual(MODULE._decode_probe_output(b""), "")
            self.assertEqual(MODULE._decode_probe_output(None), "")

    def test_real_subprocess_captures_gbk_and_invalid_bytes_without_reader_errors(self):
        stdout = "正在检查浏览器".encode("gbk")
        stderr = b"failure: \xff"
        command = [sys.executable, "-c", (
            "import os; "
            f"os.write(1, {stdout!r}); os.write(2, {stderr!r}); raise SystemExit(7)"
        )]
        with mock.patch.object(MODULE.locale, "getpreferredencoding", return_value="cp936"), \
                mock.patch.object(MODULE.sys, "platform", "linux"):
            result = MODULE._run_probe(command, timeout=15)
        self.assertEqual(result.args, command)
        self.assertEqual(result.returncode, 7)
        self.assertEqual(result.stdout, "正在检查浏览器")
        self.assertEqual(result.stderr, "failure: \ufffd")

    def test_probe_timeout_is_not_swallowed_by_decoding(self):
        with mock.patch.object(MODULE.subprocess, "run", side_effect=subprocess.TimeoutExpired("node", 15)):
            with self.assertRaises(subprocess.TimeoutExpired):
                MODULE._run_probe(["node", "--version"], timeout=15)

    def test_preflight_keeps_syntax_failure_and_checks_every_probe_in_binary_mode(self):
        args = MODULE.build_parser().parse_args(["--runtime-check"])

        def run(command, **kwargs):
            self.assertIs(kwargs["text"], False)
            output = b"v22.0.0" if command == ["node", "--version"] else b""
            code = 1 if "--check" in command else 0
            return subprocess.CompletedProcess(command, code, output, "语法错误".encode("gbk"))

        with mock.patch.object(MODULE.sys, "platform", "linux"), \
                mock.patch.object(MODULE.locale, "getpreferredencoding", return_value="cp936"), \
                mock.patch.object(MODULE, "command_path", side_effect=lambda command: command), \
                mock.patch.object(MODULE, "detect_chrome", return_value="chrome"), \
                mock.patch.object(MODULE.subprocess, "run", side_effect=run):
            result = MODULE.runtime_status(args)
        self.assertFalse(result["engineSyntaxOk"])
        self.assertFalse(result["runtimeReady"])
        self.assertEqual(len(result["engineSyntaxErrors"]), 2)
        self.assertTrue(all("语法错误" in error for error in result["engineSyntaxErrors"]))


class WindowsVersionResourceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.executable = Path(temporary.name) / "浏览器 with spaces.exe"
        self.executable.write_bytes(b"version resource supplied by test API")
        self.info = MODULE._WindowsFixedFileInfo()
        self.info.dwSignature = 0xFEEF04BD
        self.info.dwFileVersionMS = (149 << 16) | 1
        self.info.dwFileVersionLS = (7827 << 16) | 103
        self.api = mock.Mock()
        self.api.GetFileVersionInfoSizeW.return_value = ctypes.sizeof(self.info)
        self.api.GetFileVersionInfoW.return_value = 1
        self.api.VerQueryValueW.side_effect = self.query_value
        loader = mock.patch.object(
            MODULE.ctypes, "WinDLL", create=True, return_value=self.api
        )
        self.loader = loader.start()
        self.addCleanup(loader.stop)
        # A missing/corrupt version must never fall back to launching Chrome.
        process = mock.patch.object(
            MODULE.subprocess, "Popen", side_effect=AssertionError("browser launched")
        )
        process.start()
        self.addCleanup(process.stop)

    def query_value(self, data, sub_block, pointer, length):
        self.assertEqual(sub_block, "\\")
        ctypes.cast(pointer, ctypes.POINTER(ctypes.c_void_p))[0] = ctypes.addressof(self.info)
        ctypes.cast(length, ctypes.POINTER(ctypes.c_uint))[0] = ctypes.sizeof(self.info)
        return 1

    def read_version(self):
        return MODULE._windows_file_version(str(self.executable))

    def test_reads_four_part_version_from_unicode_path_without_launching(self):
        self.assertEqual(self.read_version(), (149, 1, 7827, 103))
        self.loader.assert_called_once_with("version.dll", winmode=0x00000800)
        self.api.GetFileVersionInfoSizeW.assert_called_once_with(
            str(self.executable.resolve()), None
        )

    def test_missing_file_and_directory_are_not_ready(self):
        self.executable.unlink()
        self.assertIsNone(self.read_version())
        self.executable.mkdir()
        self.assertIsNone(self.read_version())
        self.loader.assert_not_called()

    def test_unreadable_or_missing_version_resource_is_not_ready(self):
        for method in ("GetFileVersionInfoSizeW", "GetFileVersionInfoW", "VerQueryValueW"):
            with self.subTest(method=method):
                with mock.patch.object(self.api, method, return_value=0):
                    self.assertIsNone(self.read_version())
        with mock.patch.object(self.api, "GetFileVersionInfoSizeW", side_effect=OSError):
            self.assertIsNone(self.read_version())
        self.loader.side_effect = OSError("version API unavailable")
        self.assertIsNone(self.read_version())

    def test_invalid_fixed_version_resource_is_not_ready(self):
        self.info.dwSignature = 0
        self.assertIsNone(self.read_version())
        self.info.dwSignature = 0xFEEF04BD
        self.info.dwFileVersionMS = self.info.dwFileVersionLS = 0
        self.assertIsNone(self.read_version())

    def test_null_or_truncated_resource_is_not_dereferenced(self):
        for pointer_value, size in ((None, ctypes.sizeof(self.info)), (ctypes.addressof(self.info), 4)):
            def query(data, sub_block, pointer, length):
                ctypes.cast(pointer, ctypes.POINTER(ctypes.c_void_p))[0] = pointer_value
                ctypes.cast(length, ctypes.POINTER(ctypes.c_uint))[0] = size
                return 1

            with self.subTest(pointer=pointer_value, size=size):
                self.api.VerQueryValueW.side_effect = query
                self.assertIsNone(self.read_version())


class ChromePreflightTests(unittest.TestCase):
    def test_windows_runtime_check_never_executes_chrome(self):
        chrome = "C:/Program Files/Google/Chrome/Application/chrome.exe"
        args = MODULE.build_parser().parse_args(["--runtime-check"])

        def run(command, **kwargs):
            self.assertNotEqual(command[0], chrome, "preflight must not launch Chrome")
            self.assertIs(kwargs["text"], False)
            output = b"v22.0.0" if command == ["node", "--version"] else b""
            return subprocess.CompletedProcess(command, 0, output, b"")

        with mock.patch.object(MODULE.sys, "platform", "win32"), \
                mock.patch.object(MODULE, "command_path", side_effect=lambda command: command), \
                mock.patch.object(MODULE, "detect_chrome", return_value=chrome), \
                mock.patch.object(MODULE.subprocess, "run", side_effect=run) as execute, \
                mock.patch.object(MODULE, "_windows_file_version") as version:
            for value, expected in (((149, 0, 7827, 103), True), (None, False)):
                with self.subTest(version=value):
                    version.return_value = value
                    result = MODULE.runtime_status(args)
                    self.assertEqual(result["chromeRunnable"], expected)
                    self.assertEqual(result["runtimeReady"], expected)
                    self.assertTrue(result["ytDlpRunnable"])
                    self.assertEqual(result["chrome"], chrome)
            version.assert_called_with(chrome)
            self.assertTrue(any(call.args[0][-1] == "--version" for call in execute.call_args_list))

    def test_non_windows_keeps_existing_version_probe_and_timeout(self):
        for platform in ("darwin", "linux"):
            with self.subTest(platform=platform), \
                    mock.patch.object(MODULE.sys, "platform", platform), \
                    mock.patch.object(MODULE, "_windows_file_version") as windows_version, \
                    mock.patch.object(MODULE.subprocess, "run") as run:
                run.return_value = subprocess.CompletedProcess([], 0)
                self.assertTrue(MODULE._chrome_version_ok("chrome"))
                run.assert_called_once_with(
                    ["chrome", "--version"], capture_output=True, text=False,
                    check=False, cwd=None, timeout=15,
                )
                run.return_value = subprocess.CompletedProcess([], 1)
                self.assertFalse(MODULE._chrome_version_ok("chrome"))
                run.side_effect = subprocess.TimeoutExpired("chrome", 15)
                self.assertFalse(MODULE._chrome_version_ok("chrome"))
                windows_version.assert_not_called()

    def test_missing_chrome_is_not_ready_on_any_platform(self):
        with mock.patch.object(MODULE.subprocess, "Popen") as process:
            for platform in ("win32", "darwin", "linux"):
                with mock.patch.object(MODULE.sys, "platform", platform):
                    self.assertFalse(MODULE._chrome_version_ok(None))
            process.assert_not_called()


@unittest.skipUnless(sys.platform == "win32", "requires native Windows version API")
class NativeWindowsVersionTests(unittest.TestCase):
    def test_real_executable_resource_with_spaces_and_unicode(self):
        with tempfile.TemporaryDirectory() as temporary:
            copied = Path(temporary) / "版本 test.exe"
            shutil.copyfile(sys.executable, copied)
            with mock.patch.object(MODULE.subprocess, "Popen", side_effect=AssertionError("process launched")):
                version = MODULE._windows_file_version(str(copied))
                self.assertIsNotNone(version)
                self.assertEqual(version[:2], (sys.version_info.major, sys.version_info.minor))
                copied.write_bytes(b"not a Windows executable")
                self.assertIsNone(MODULE._windows_file_version(str(copied)))

    def test_installed_chrome_preflight_does_not_launch(self):
        chrome = MODULE.detect_chrome(None)
        self.assertIsNotNone(chrome, "Windows smoke test requires installed Chrome/Chromium")
        with mock.patch.object(MODULE.subprocess, "Popen", side_effect=AssertionError("Chrome launched")):
            for _ in range(3):
                self.assertTrue(MODULE._chrome_version_ok(chrome))


if __name__ == "__main__":
    unittest.main()
