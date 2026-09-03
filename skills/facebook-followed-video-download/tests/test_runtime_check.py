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
            output = "v22.0.0" if command == ["node", "--version"] else ""
            return subprocess.CompletedProcess(command, 0, output, "")

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
                    ["chrome", "--version"], capture_output=True, text=True,
                    check=False, timeout=15,
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
