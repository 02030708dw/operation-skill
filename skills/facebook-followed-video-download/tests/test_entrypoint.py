import importlib.util
import json
import tempfile
import unittest
import shutil
import subprocess
from unittest import mock
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
    @unittest.skipUnless(shutil.which('node'), 'Node required for persistent metric integration')
    def test_failed_engine_metrics_survive_temporary_result_cleanup(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            accounts = root / 'accounts.txt'
            accounts.write_text('creator\thttps://www.facebook.com/example/reels/\n')
            result = root / 'execution/download.json'
            args = MODULE.build_parser().parse_args(['--result-json', str(result), '--no-report'])
            original_popen = subprocess.Popen
            node = shutil.which('node')
            temporary_results = []
            helper = str(MODULE.SCRIPT_DIR / 'capture_observability.js')
            def failed_engine(command, **kwargs):
                temporary_results.append(Path(command[command.index('--result-json') + 1]))
                script = "const args=process.argv; const target=args[args.indexOf('--metrics-result-json')+1]; require(" + json.dumps(helper) + ").createMetrics(target).record('navigation','failure',1,'NETWORK_ERROR'); process.exit(1);"
                return original_popen([node, '-e', script, '--', *command[2:]], **kwargs)
            with mock.patch.object(MODULE, 'dependency_status', return_value={'ready_for_preview': True, 'node': shutil.which('node')}), mock.patch.object(MODULE, 'enabled_browser_profile', return_value=None), mock.patch.dict(MODULE.os.environ, {}, clear=True), mock.patch.object(MODULE.subprocess, 'Popen', side_effect=failed_engine):
                self.assertEqual(MODULE._run_download_with_accounts(args, accounts), 1)
            self.assertFalse(temporary_results[0].parent.exists())
            metric = json.loads((result.parent / 'process-metrics.jsonl').read_text())
            self.assertEqual(metric['errorCode'], 'NETWORK_ERROR')
            self.assertEqual(json.loads(result.read_text())['exitCode'], 1)

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

    def test_single_run_lock_rejects_a_competing_nonblocking_run(self):
        with tempfile.TemporaryDirectory() as temporary:
            lock_path = Path(temporary) / "capture.lock"
            with MODULE.single_run_lock(lock_path):
                with self.assertRaises(MODULE.ConcurrentRunError):
                    with MODULE.single_run_lock(lock_path, wait=False):
                        pass
            with MODULE.single_run_lock(lock_path, wait=False):
                pass


if __name__ == "__main__":
    unittest.main()
