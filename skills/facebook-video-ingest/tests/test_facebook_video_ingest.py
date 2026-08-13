import importlib.util
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock


MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "facebook_video_ingest.py"
)
SPEC = importlib.util.spec_from_file_location("facebook_video_ingest", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MODULE)

RUNNER_PATH = MODULE_PATH.with_name("hermes_cron_runner.py")
RUNNER_SPEC = importlib.util.spec_from_file_location("hermes_cron_runner", RUNNER_PATH)
RUNNER = importlib.util.module_from_spec(RUNNER_SPEC)
assert RUNNER_SPEC and RUNNER_SPEC.loader
RUNNER_SPEC.loader.exec_module(RUNNER)

INSTALLER_PATH = MODULE_PATH.with_name("install_hermes_worker.py")
INSTALLER_SPEC = importlib.util.spec_from_file_location(
    "install_hermes_worker", INSTALLER_PATH
)
INSTALLER = importlib.util.module_from_spec(INSTALLER_SPEC)
assert INSTALLER_SPEC and INSTALLER_SPEC.loader
INSTALLER_SPEC.loader.exec_module(INSTALLER)


class PipelineTests(unittest.TestCase):
    def test_installer_allows_production_admin_origin_by_default(self):
        self.assertIn(
            "https://hermes.mvkbmb.online",
            INSTALLER.DEFAULT_ADMIN_ORIGINS,
        )

    def test_daily_recent_video_target_defaults_to_ten(self):
        args = MODULE.build_parser().parse_args(["--check"])
        self.assertEqual(args.count, 10)

    def test_worker_loads_private_hermes_env_without_overwriting_process(self):
        with tempfile.TemporaryDirectory() as temporary:
            env_file = Path(temporary) / ".env"
            env_file.write_text(
                "HM_BACKEND_URL='https://live.example.com/hm'\n"
                "export HM_WORKER_ID=worker-from-file\n",
                encoding="utf-8",
            )
            with mock.patch.dict(
                os.environ,
                {"HM_WORKER_ID": "worker-from-process"},
                clear=True,
            ):
                MODULE.load_env_file(env_file)
                self.assertEqual(
                    os.environ["HM_BACKEND_URL"], "https://live.example.com/hm"
                )
                self.assertEqual(os.environ["HM_WORKER_ID"], "worker-from-process")

    def test_cron_runner_resolves_only_standard_installed_skill(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            expected = (
                home
                / "skills"
                / "facebook-video-ingest"
                / "scripts"
                / "facebook_video_ingest.py"
            )
            categorized = (
                home
                / "skills"
                / "operation-skill"
                / "facebook-video-ingest"
                / "scripts"
                / "facebook_video_ingest.py"
            )
            expected.parent.mkdir(parents=True)
            categorized.parent.mkdir(parents=True)
            expected.write_text("# current\n", encoding="utf-8")
            categorized.write_text("# categorized\n", encoding="utf-8")
            self.assertEqual(RUNNER.find_worker_script(home), expected.resolve())
            expected.unlink()
            with self.assertRaises(FileNotFoundError):
                RUNNER.find_worker_script(home)

    def test_cron_runner_loads_private_hermes_env_without_overwriting_process(self):
        with tempfile.TemporaryDirectory() as temporary:
            env_file = Path(temporary) / ".env"
            env_file.write_text(
                "# comment\nHM_BACKEND_URL='https://backend.example.com'\n"
                "export HM_WORKER_ID=worker-from-file\n",
                encoding="utf-8",
            )
            with mock.patch.dict(
                os.environ,
                {"HM_WORKER_ID": "worker-from-process"},
                clear=True,
            ):
                RUNNER.load_env_file(env_file)
                self.assertEqual(
                    os.environ["HM_BACKEND_URL"], "https://backend.example.com"
                )
                self.assertEqual(os.environ["HM_WORKER_ID"], "worker-from-process")

    @unittest.skipIf(os.name == "nt", "POSIX lock behavior is covered on this host")
    def test_cron_runner_prevents_overlapping_workers(self):
        with tempfile.TemporaryDirectory() as temporary:
            lock_path = Path(temporary) / "worker.lock"
            first = RUNNER.acquire_worker_lock(lock_path)
            self.assertIsNotNone(first)
            try:
                self.assertIsNone(RUNNER.acquire_worker_lock(lock_path))
            finally:
                first.close()
            second = RUNNER.acquire_worker_lock(lock_path)
            self.assertIsNotNone(second)
            second.close()

    def test_task_specific_cron_runner_targets_its_backend_task(self):
        command = RUNNER.worker_command(
            Path("/tmp/facebook_video_ingest.py"),
            Path("/tmp/hm_capture_C-5786859AED6E_1400.py"),
        )
        self.assertEqual(
            command,
            [
                RUNNER.sys.executable,
                "/tmp/facebook_video_ingest.py",
                "--execute",
                "--task-no",
                "C-5786859AED6E",
                "--wait-for-work-seconds",
                "30",
                "--json",
            ],
        )
        home = Path("/tmp/hermes")
        self.assertEqual(
            RUNNER.worker_lock_path(
                home,
                Path("/tmp/hm_capture_C-5786859AED6E_1430.py"),
            ),
            home / "facebook-video-ingest" / "worker-C-5786859AED6E.lock",
        )
        self.assertEqual(
            RUNNER.task_no_from_runner(
                Path("/tmp/hm_capture_C-5786859AED6E_immediate.py")
            ),
            "C-5786859AED6E",
        )
        exact_command = RUNNER.worker_command(
            Path("/tmp/facebook_video_ingest.py"),
            Path("/tmp/hm_capture_C-5786859AED6E_E-A6B3318C9D634BF8.py"),
        )
        self.assertIn("--execution-no", exact_command)
        self.assertIn("E-A6B3318C9D634BF8", exact_command)
        self.assertEqual(
            RUNNER.worker_lock_path(
                home,
                Path("/tmp/hm_capture_C-5786859AED6E_E-A6B3318C9D634BF8.py"),
            ),
            home / "facebook-video-ingest" / "worker-E-A6B3318C9D634BF8.lock",
        )

    def test_shared_cron_runner_starts_continuous_customer_side_worker(self):
        self.assertEqual(
            RUNNER.worker_command(
                Path("/tmp/facebook_video_ingest.py"),
                Path("/tmp/hm_facebook_video_ingest_worker.py"),
            ),
            [
                RUNNER.sys.executable,
                "/tmp/facebook_video_ingest.py",
                "--watch",
            ],
        )

    def test_installer_detects_legacy_shared_job_and_supervised_gateway(self):
        listing = "  abc123 [active]\n    Name:      HM 视频抓取 Worker\n"
        self.assertTrue(INSTALLER.has_legacy_job(listing))
        self.assertTrue(
            INSTALLER.gateway_is_running(
                "✓ Gateway is supervised by launchd (PID 123)"
            )
        )
        self.assertTrue(
            INSTALLER.gateway_is_running(
                "✓ Gateway process running (PID: 11864)"
            )
        )
        self.assertTrue(
            INSTALLER.gateway_is_running(
                "✓ Gateway already running (PID: 11864)"
            )
        )
        self.assertFalse(INSTALLER.gateway_is_running("✗ Gateway is not running"))
        self.assertFalse(INSTALLER.gateway_is_running("✗ No gateway process detected"))
        receiver_listing = (
            "  def456 [active]\n"
            "    Name:      HM 后台任务接收 Worker\n"
        )
        self.assertTrue(
            INSTALLER.has_job(receiver_listing, INSTALLER.WORKER_JOB_NAME)
        )

    def test_installer_decodes_hermes_output_as_utf8_on_windows(self):
        completed = INSTALLER.subprocess.CompletedProcess(
            ["hermes", "gateway", "status"],
            0,
            stdout="✓ Gateway is running\n",
            stderr="",
        )
        with mock.patch.object(
            INSTALLER.subprocess,
            "run",
            return_value=completed,
        ) as run:
            INSTALLER.call(["hermes", "gateway", "status"])

        self.assertEqual(run.call_args.kwargs["encoding"], "utf-8")
        self.assertEqual(run.call_args.kwargs["errors"], "replace")
        self.assertTrue(run.call_args.kwargs["capture_output"])

    def test_installer_keeps_console_attached_for_windows_uac(self):
        completed = INSTALLER.subprocess.CompletedProcess(
            ["hermes", "gateway", "install"],
            0,
        )
        with mock.patch.object(
            INSTALLER.subprocess,
            "run",
            return_value=completed,
        ) as run:
            INSTALLER.call(
                ["hermes", "gateway", "install"],
                interactive=True,
            )

        self.assertFalse(run.call_args.kwargs["capture_output"])

    def test_installer_removes_only_continuous_receiver_jobs(self):
        listing = (
            "  a [active]\n"
            "    Name:      HM 视频抓取 C-001 14:00\n"
            "  b [completed]\n"
            "    Name:      HM 立即抓取 C-001 E-001\n"
            "  c [active]\n"
            "    Name:      HM 后台任务接收 Worker\n"
            "  d [active]\n"
            "    Name:      HM 视频抓取 Worker\n"
        )
        self.assertEqual(
            INSTALLER.obsolete_job_names(listing),
            [
                "HM 立即抓取 C-001 E-001",
                "HM 后台任务接收 Worker",
                "HM 视频抓取 Worker",
            ],
        )

    def test_installer_configures_loopback_api_without_overwriting_other_env(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            env_file = home / ".env"
            env_file.write_text(
                "HM_WORKER_TOKEN=keep-secret\nAPI_SERVER_KEY=existing-long-key\n",
                encoding="utf-8",
            )
            key, url = INSTALLER.configure_api_server(
                home,
                port=8642,
                admin_origins=["https://admin.example.com/"],
            )

            values = INSTALLER.parse_env_file(env_file)
            self.assertEqual(key, "existing-long-key")
            self.assertEqual(url, "http://127.0.0.1:8642")
            self.assertEqual(values["HM_WORKER_TOKEN"], "keep-secret")
            self.assertEqual(values["API_SERVER_HOST"], "127.0.0.1")
            self.assertEqual(values["API_SERVER_PORT"], "8642")
            self.assertEqual(values["HERMES_ACCEPT_HOOKS"], "1")
            self.assertEqual(
                values["API_SERVER_CORS_ORIGINS"],
                "https://admin.example.com",
            )

    def test_installer_preserves_existing_cors_origins_on_reinstall(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            env_file = home / ".env"
            env_file.write_text(
                "API_SERVER_KEY=existing-long-key\n"
                "API_SERVER_CORS_ORIGINS=https://customer.example.com\n",
                encoding="utf-8",
            )

            INSTALLER.configure_api_server(
                home,
                port=8642,
                admin_origins=[
                    "https://hermes.mvkbmb.online",
                    "https://customer.example.com/",
                ],
            )

            values = INSTALLER.parse_env_file(env_file)
            self.assertEqual(
                values["API_SERVER_CORS_ORIGINS"],
                "https://hermes.mvkbmb.online,https://customer.example.com",
            )

    def test_installer_pairing_code_contains_only_local_api_configuration(self):
        code = INSTALLER.pairing_code("http://127.0.0.1:8642", "a" * 32)
        self.assertTrue(code.startswith(INSTALLER.PAIRING_PREFIX))
        encoded = code[len(INSTALLER.PAIRING_PREFIX) :]
        encoded += "=" * ((4 - len(encoded) % 4) % 4)
        payload = json.loads(
            INSTALLER.base64.urlsafe_b64decode(encoded).decode("utf-8")
        )
        self.assertEqual(
            payload,
            {"apiBaseUrl": "http://127.0.0.1:8642", "apiKey": "a" * 32},
        )

    @unittest.skipIf(os.name == "nt", "POSIX lock behavior is covered on this host")
    def test_watch_mode_allows_only_one_customer_side_worker(self):
        with tempfile.TemporaryDirectory() as temporary:
            lock_path = Path(temporary) / "watch.lock"
            first = MODULE.acquire_watch_lock(lock_path)
            self.assertIsNotNone(first)
            try:
                self.assertIsNone(MODULE.acquire_watch_lock(lock_path))
            finally:
                first.close()

    def test_manifest_videos_inherits_source_name(self):
        videos = MODULE.manifest_videos(
            {
                "sources": [
                    {
                        "name": "creator-one",
                        "videos": [{"platformVideoId": "123", "status": "downloaded"}],
                    }
                ]
            }
        )
        self.assertEqual(videos[0]["source"], "creator-one")

    def test_upload_status_mapping_is_explicit(self):
        self.assertEqual(MODULE.normalized_upload_status("uploaded"), "UPLOADED")
        self.assertEqual(
            MODULE.normalized_upload_status("skipped-existing"),
            "SKIPPED_EXISTING",
        )
        self.assertEqual(MODULE.normalized_upload_status("conflict"), "R2_CONFLICT")
        self.assertEqual(MODULE.normalized_upload_status("unexpected"), "UPLOAD_FAILED")

    def test_backend_r2_prefix_uses_region_category_and_execution_date(self):
        self.assertEqual(
            MODULE.job_r2_prefix(
                {"r2Prefix": "PH/Sports/202608/10"},
                "legacy/prefix",
            ),
            "PH/Sports/202608/10",
        )

    def test_worker_token_has_no_command_line_argument(self):
        with mock.patch.dict(os.environ, {"HM_WORKER_TOKEN": "secret"}):
            parser = MODULE.build_parser()
            args = parser.parse_args(["--check"])
        self.assertEqual(args.worker_token, "secret")
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            parser.parse_args(["--worker-token", "secret", "--check"])

    def test_worker_uses_explicit_cloudflare_safe_user_agent(self):
        captured = {}

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return b'{"code":200,"message":"success","data":null}'

        def fake_urlopen(outgoing, timeout):
            captured.update(dict(outgoing.header_items()))
            return Response()

        with mock.patch.object(MODULE.request, "urlopen", side_effect=fake_urlopen):
            MODULE.api_call(
                "https://live.example.com/hm",
                "secret",
                "POST",
                "/api/internal/capture/executions/claim",
                {"workerId": "worker-01"},
            )

        self.assertEqual(captured["User-agent"], MODULE.WORKER_USER_AGENT)
        self.assertEqual(captured["Accept"], "application/json")

    def test_no_work_is_successful(self):
        args = MODULE.build_parser().parse_args(
            [
                "--execute",
                "--backend",
                "https://backend.example.com",
                "--worker-id",
                "worker-01",
            ]
        )
        args.worker_token = "secret"
        with mock.patch.object(MODULE, "claim", return_value=None):
            exit_code, result = MODULE.execute_one(args)
        self.assertEqual(exit_code, 0)
        self.assertEqual(result["status"], "no-work")

    def test_targeted_claim_sends_backend_task_number(self):
        captured = {}

        def fake_api_call(backend, token, method, path, payload, **kwargs):
            captured.update(payload)
            return None

        with mock.patch.object(MODULE, "api_call", side_effect=fake_api_call):
            MODULE.claim(
                "https://backend.example.com",
                "secret",
                "worker-01",
                "c-001",
                "e-001",
            )

        self.assertEqual(captured["taskNo"], "C-001")
        self.assertEqual(captured["executionNo"], "E-001")

    def test_record_video_sends_hash_and_r2_fields(self):
        captured = {}

        def fake_api_call(backend, token, method, path, payload, **kwargs):
            captured.update(payload)

        video = {
            "originalUrl": "https://www.facebook.com/reel/123",
            "canonicalUrl": "https://www.facebook.com/reel/123",
            "fileName": "video.mp4",
            "sha256": "a" * 64,
            "r2Bucket": "media",
            "r2ObjectKey": "facebook/source/video.mp4",
        }
        with mock.patch.object(MODULE, "api_call", side_effect=fake_api_call):
            MODULE.record_video(
                "https://backend.example.com",
                "secret",
                "worker-01",
                "E-001",
                video,
                download_status="DOWNLOADED",
                upload_status="UPLOADED",
            )
        self.assertEqual(captured["fileSha256"], "a" * 64)
        self.assertEqual(captured["r2ObjectKey"], "facebook/source/video.mp4")

    def test_execute_one_composes_download_upload_and_callbacks(self):
        state = tempfile.TemporaryDirectory()
        self.addCleanup(state.cleanup)
        args = MODULE.build_parser().parse_args(
            [
                "--execute",
                "--backend",
                "https://backend.example.com",
                "--worker-id",
                "worker-01",
                "--state-dir",
                state.name,
            ]
        )
        args.worker_token = "secret"
        download_video = {
            "source": "C-001",
            "platformVideoId": "123",
            "originalUrl": "https://www.facebook.com/reel/123",
            "canonicalUrl": "https://www.facebook.com/reel/123",
            "localPath": "/tmp/video.mp4",
            "fileName": "video.mp4",
            "fileSize": 10,
            "sha256": "a" * 64,
            "status": "downloaded",
        }

        commands = []

        def fake_run(command):
            commands.append(command)
            result_path = Path(command[command.index("--result-json") + 1])
            if "facebook_followed_video_download.py" in command[1]:
                payload = {
                    "status": "completed",
                    "sources": [{"name": "C-001", "videos": [download_video]}],
                }
            else:
                uploaded = dict(download_video)
                uploaded.update(
                    {
                        "status": "uploaded",
                        "r2Bucket": "media",
                        "r2ObjectKey": "facebook/C-001/video.mp4",
                    }
                )
                payload = {"status": "completed", "videos": [uploaded]}
            result_path.write_text(json.dumps(payload), encoding="utf-8")
            return 0, "ok\n"

        job = {
            "executionId": "E-001",
            "accountName": "PH Sports Official",
            "region": "PH",
            "category": "Sports",
            "r2Prefix": "PH/Sports/202608/10",
            "sourceName": "C-001",
            "sourceUrl": "https://www.facebook.com/example/reels/",
        }
        with (
            mock.patch.object(MODULE, "claim", return_value=job),
            mock.patch.object(MODULE, "heartbeat"),
            mock.patch.object(MODULE, "run_command", side_effect=fake_run),
            mock.patch.object(MODULE, "record_video") as record,
            mock.patch.object(MODULE, "complete") as complete,
        ):
            exit_code, result = MODULE.execute_one(args)

        self.assertEqual(exit_code, 0)
        self.assertEqual(result["status"], "COMPLETED")
        self.assertEqual(record.call_count, 2)
        self.assertEqual(complete.call_args.args[4], "COMPLETED")
        upload_command = commands[1]
        self.assertEqual(
            upload_command[upload_command.index("--prefix") + 1],
            "PH/Sports/202608/10",
        )
        self.assertIn("--flatten", upload_command)
        download_command = commands[0]
        self.assertEqual(
            download_command[download_command.index("--initial-count") + 1],
            "10",
        )

    def test_partial_business_result_is_a_successful_cron_run(self):
        state = tempfile.TemporaryDirectory()
        self.addCleanup(state.cleanup)
        args = MODULE.build_parser().parse_args(
            [
                "--execute",
                "--backend",
                "https://backend.example.com",
                "--worker-id",
                "worker-01",
                "--state-dir",
                state.name,
            ]
        )
        args.worker_token = "secret"
        downloaded = {
            "source": "C-001",
            "platformVideoId": "123",
            "originalUrl": "https://www.facebook.com/reel/123",
            "localPath": "/tmp/video.mp4",
            "fileName": "video.mp4",
            "status": "downloaded",
        }
        failed = {
            "source": "C-001",
            "platformVideoId": "1",
            "originalUrl": "https://www.facebook.com/watch/?v=1",
            "status": "download-failed",
            "error": "invalid video",
        }

        def fake_run(command):
            result_path = Path(command[command.index("--result-json") + 1])
            if "facebook_followed_video_download.py" in command[1]:
                payload = {
                    "status": "partial",
                    "sources": [
                        {"name": "C-001", "videos": [downloaded, failed]}
                    ],
                }
            else:
                uploaded = dict(downloaded)
                uploaded["status"] = "uploaded"
                payload = {"status": "completed", "videos": [uploaded]}
            result_path.write_text(json.dumps(payload), encoding="utf-8")
            return 0, "ok\n"

        with (
            mock.patch.object(
                MODULE,
                "claim",
                return_value={
                    "executionId": "E-PARTIAL",
                    "accountName": "PH Sports Official",
                    "region": "PH",
                    "category": "Sports",
                    "r2Prefix": "PH/Sports/202608/10",
                    "sourceName": "C-001",
                    "sourceUrl": "https://www.facebook.com/example/reels/",
                },
            ),
            mock.patch.object(MODULE, "heartbeat"),
            mock.patch.object(MODULE, "run_command", side_effect=fake_run),
            mock.patch.object(MODULE, "record_video"),
            mock.patch.object(MODULE, "complete") as complete,
        ):
            exit_code, result = MODULE.execute_one(args)

        self.assertEqual(exit_code, 0)
        self.assertEqual(result["status"], "PARTIAL")
        self.assertEqual(complete.call_args.args[4], "PARTIAL")

    def test_reuses_verified_durable_download_manifest(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            video = root / "video.mp4"
            video.write_bytes(b"video")
            manifest = root / "download.json"
            manifest.write_text(
                json.dumps(
                    {
                        "status": "completed",
                        "sources": [
                            {
                                "name": "C-001",
                                "videos": [
                                    {
                                        "status": "downloaded",
                                        "localPath": str(video),
                                        "fileSize": video.stat().st_size,
                                    }
                                ],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            self.assertIsNotNone(MODULE.reusable_download_result(manifest))
            video.write_bytes(b"changed")
            self.assertIsNone(MODULE.reusable_download_result(manifest))

    def test_backend_callback_failure_leaves_execution_for_lease_retry(self):
        args = MODULE.build_parser().parse_args(
            [
                "--execute",
                "--backend",
                "https://backend.example.com",
                "--worker-id",
                "worker-01",
            ]
        )
        args.worker_token = "secret"
        job = {
            "executionId": "E-001",
            "accountName": "PH Sports Official",
            "region": "PH",
            "category": "Sports",
            "r2Prefix": "PH/Sports/202608/10",
            "sourceName": "C-001",
            "sourceUrl": "https://www.facebook.com/example/reels/",
        }
        with (
            mock.patch.object(MODULE, "claim", return_value=job),
            mock.patch.object(
                MODULE,
                "heartbeat",
                side_effect=MODULE.BackendError("backend unavailable"),
            ),
            mock.patch.object(MODULE, "complete") as complete,
        ):
            exit_code, result = MODULE.execute_one(args)
        self.assertEqual(exit_code, 1)
        self.assertEqual(result["retry"], "lease")
        complete.assert_not_called()


if __name__ == "__main__":
    unittest.main()
