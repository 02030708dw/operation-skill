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

EXTENSION_PATH = MODULE_PATH.with_name("hm_capture_gateway_extension.py")
EXTENSION_SPEC = importlib.util.spec_from_file_location(
    "hm_capture_gateway_extension", EXTENSION_PATH
)
EXTENSION = importlib.util.module_from_spec(EXTENSION_SPEC)
assert EXTENSION_SPEC and EXTENSION_SPEC.loader
EXTENSION_SPEC.loader.exec_module(EXTENSION)


class PipelineTests(unittest.TestCase):
    def test_installer_allows_production_admin_origin_by_default(self):
        self.assertIn(
            "https://hermes.mvkbmb.online",
            INSTALLER.DEFAULT_ADMIN_ORIGINS,
        )

    def test_gateway_extension_materializes_exact_no_agent_runner(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            scripts = home / "scripts"
            scripts.mkdir()
            (scripts / EXTENSION.BASE_RUNNER_NAME).write_text(
                "# trusted runner\n", encoding="utf-8"
            )
            prepared = EXTENSION.prepare_capture_job_body(
                {
                    "name": "HM immediate",
                    "hm_capture_runner": {
                        "taskNo": "C-5786859AED6E",
                        "executionNo": "E-A6B3318C9D634BF8",
                    },
                },
                home=home,
            )
            expected = "hm_capture_C-5786859AED6E_E-A6B3318C9D634BF8.py"
            self.assertTrue(prepared["no_agent"])
            self.assertEqual(prepared["script"], expected)
            self.assertEqual(prepared["skills"], [])
            self.assertEqual(
                (scripts / expected).read_text(encoding="utf-8"),
                "# trusted runner\n",
            )
            EXTENSION.cleanup_capture_job_script(prepared, home=home)
            self.assertFalse((scripts / expected).exists())

    def test_gateway_extension_rejects_ambiguous_runner_target(self):
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(ValueError):
                EXTENSION.prepare_capture_job_body(
                    {
                        "hm_capture_runner": {
                            "taskNo": "C-5786859AED6E",
                            "executionNo": "E-A6B3318C9D634BF8",
                            "scheduleKey": "1400",
                        }
                    },
                    home=Path(temporary),
                )

    def test_gateway_extension_materializes_approved_upload_runner(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            scripts = home / "scripts"
            scripts.mkdir()
            (scripts / EXTENSION.BASE_RUNNER_NAME).write_text(
                "# trusted runner\n", encoding="utf-8"
            )
            prepared = EXTENSION.prepare_capture_job_body(
                {
                    "hm_capture_runner": {
                        "taskNo": "C-5786859AED6E",
                        "uploadVideoNo": "V-ABC123",
                    }
                },
                home=home,
            )
            self.assertEqual(
                prepared["script"],
                "hm_capture_upload_C-5786859AED6E_V-ABC123.py",
            )

    def test_installer_gateway_api_patch_is_idempotent(self):
        api_server = (
            Path.home()
            / ".hermes"
            / "hermes-agent"
            / "gateway"
            / "platforms"
            / "api_server.py"
        )
        if not api_server.is_file():
            self.skipTest("Hermes API server source is not installed")
        original = api_server.read_text(encoding="utf-8")
        patched = INSTALLER.patch_gateway_api_source(original)
        compile(patched, str(api_server), "exec")
        self.assertEqual(INSTALLER.patch_gateway_api_source(patched), patched)
        self.assertIn(
            "prepare_capture_job_body(await request.json())", patched
        )
        self.assertIn(
            '("DELETE", "/api/hm-capture/video", self._handle_delete_hm_capture_video)',
            patched,
        )
        self.assertIn("_check_hm_capture_media_auth", patched)
        self.assertIn('os.getenv("HM_CAPTURE_MEDIA_TOKEN"', patched)

    def test_gateway_extension_deletes_only_allowed_video_path(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            video = (
                home
                / "facebook-video-ingest"
                / "executions"
                / "E-001"
                / "video.mp4"
            )
            video.parent.mkdir(parents=True)
            video.write_bytes(b"video")

            deleted = EXTENSION.delete_capture_video_path(video, home=home)

            self.assertEqual(deleted, video.resolve())
            self.assertFalse(video.exists())
            outside = home / "outside.mp4"
            outside.write_bytes(b"video")
            with self.assertRaises(PermissionError):
                EXTENSION.delete_capture_video_path(outside, home=home)
            self.assertTrue(outside.exists())

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
            exact_command[exact_command.index("--wait-for-work-seconds") + 1],
            "90",
        )
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

    def test_approved_upload_runner_skips_capture_claim(self):
        runner = Path("/tmp/hm_capture_upload_C-001_V-001.py")
        command = RUNNER.worker_command(
            Path("/tmp/facebook_video_ingest.py"),
            runner,
        )
        self.assertEqual(
            command,
            [
                RUNNER.sys.executable,
                "/tmp/facebook_video_ingest.py",
                "--execute",
                "--upload-only",
                "--task-no",
                "C-001",
                "--json",
            ],
        )
        self.assertEqual(
            RUNNER.worker_lock_path(Path("/tmp/hermes"), runner),
            Path("/tmp/hermes/facebook-video-ingest/upload-worker.lock"),
        )

    def test_recurring_upload_queue_runner_only_drains_approved_uploads(self):
        home = Path("/tmp/hermes")
        runner = Path("/tmp/hm_capture_upload_worker.py")
        self.assertEqual(
            RUNNER.worker_command(
                Path("/tmp/facebook_video_ingest.py"), runner
            ),
            [
                RUNNER.sys.executable,
                "/tmp/facebook_video_ingest.py",
                "--execute",
                "--upload-only",
                "--json",
            ],
        )
        self.assertEqual(
            RUNNER.worker_lock_path(home, runner),
            home / "facebook-video-ingest" / "upload-worker.lock",
        )

    def test_installer_installs_recurring_upload_runner(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            INSTALLER.install_runner(home)
            self.assertEqual(
                (home / "scripts" / INSTALLER.UPLOAD_RUNNER_NAME).read_bytes(),
                RUNNER_PATH.read_bytes(),
            )

    def test_installer_creates_upload_queue_worker_job(self):
        calls = []

        def fake_api(base, key, method, path, payload=None):
            calls.append((method, path, payload))
            if method == "GET":
                return {"jobs": []}
            return {
                "job": {
                    "id": "upload-worker",
                    "name": INSTALLER.UPLOAD_WORKER_JOB_NAME,
                    "schedule_display": INSTALLER.UPLOAD_WORKER_SCHEDULE,
                    "enabled": True,
                    "script": INSTALLER.UPLOAD_RUNNER_NAME,
                    "no_agent": True,
                }
            }

        with mock.patch.object(INSTALLER, "local_api_json", side_effect=fake_api):
            job = INSTALLER.ensure_upload_worker_job(
                "http://127.0.0.1:8642", "secret"
            )

        self.assertEqual(job["script"], INSTALLER.UPLOAD_RUNNER_NAME)
        self.assertEqual(calls[-1][0:2], ("POST", "/api/jobs"))
        self.assertEqual(calls[-1][2]["schedule"], "* * * * *")

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

    def test_installer_configures_lan_media_and_keeps_loopback_pairing(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            env_file = home / ".env"
            env_file.write_text(
                "HM_WORKER_TOKEN=keep-secret\nAPI_SERVER_KEY=existing-long-key\n",
                encoding="utf-8",
            )
            key, url, media_url = INSTALLER.configure_api_server(
                home,
                port=8642,
                admin_origins=["https://admin.example.com/"],
                worker_id="worker-01",
                worker_token="keep-secret",
                backend="http://192.168.1.10:6200",
                media_base_url="http://192.168.1.20:8642",
            )

            values = INSTALLER.parse_env_file(env_file)
            self.assertEqual(key, "existing-long-key")
            self.assertEqual(url, "http://127.0.0.1:8642")
            self.assertEqual(media_url, "http://192.168.1.20:8642")
            self.assertEqual(values["HM_WORKER_TOKEN"], "keep-secret")
            self.assertEqual(values["API_SERVER_HOST"], "0.0.0.0")
            self.assertEqual(values["API_SERVER_PORT"], "8642")
            self.assertEqual(
                values["HM_CAPTURE_MEDIA_TOKEN"],
                INSTALLER.derive_media_token("keep-secret", "worker-01"),
            )
            self.assertEqual(
                values["HM_CAPTURE_MEDIA_BASE_URL"],
                "http://192.168.1.20:8642",
            )
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
                worker_id="worker-01",
                worker_token="keep-secret",
                backend="http://192.168.1.10:6200",
                media_base_url="http://192.168.1.20:8642",
            )

            values = INSTALLER.parse_env_file(env_file)
            self.assertEqual(
                values["API_SERVER_CORS_ORIGINS"],
                "https://hermes.mvkbmb.online,https://customer.example.com",
            )

    def test_installer_rejects_public_media_address(self):
        with self.assertRaises(ValueError):
            INSTALLER.normalize_media_base_url(
                "http://8.8.8.8:8642",
                backend="https://backend.example.com",
                port=8642,
            )

    def test_installer_pairing_code_contains_only_local_api_configuration(self):
        code = INSTALLER.pairing_code(
            "http://127.0.0.1:8642", "a" * 32, "worker-01"
        )
        self.assertTrue(code.startswith(INSTALLER.PAIRING_PREFIX))
        encoded = code[len(INSTALLER.PAIRING_PREFIX) :]
        encoded += "=" * ((4 - len(encoded) % 4) % 4)
        payload = json.loads(
            INSTALLER.base64.urlsafe_b64decode(encoded).decode("utf-8")
        )
        self.assertEqual(
            payload,
            {
                "apiBaseUrl": "http://127.0.0.1:8642",
                "apiKey": "a" * 32,
                "workerId": "worker-01",
            },
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

    def test_downloader_video_event_is_parsed_without_accepting_normal_logs(self):
        video = {
            "status": "downloaded",
            "canonicalUrl": "https://www.facebook.com/reel/123",
        }
        line = MODULE.VIDEO_RESULT_EVENT_PREFIX + json.dumps(
            {
                "schemaVersion": "1.0",
                "event": "video-result",
                "source": "creator-one",
                "completed": 2,
                "total": 30,
                "video": video,
            }
        )

        parsed = MODULE.parse_download_video_event(line)

        self.assertEqual(parsed["video"]["source"], "creator-one")
        self.assertEqual(parsed["completed"], 2)
        self.assertIsNone(MODULE.parse_download_video_event("normal log line"))
        self.assertIsNone(
            MODULE.parse_download_video_event(
                MODULE.VIDEO_RESULT_EVENT_PREFIX + "not-json"
            )
        )

    def test_stream_callback_failure_is_retried_from_final_manifest(self):
        video = {
            "status": "downloaded",
            "originalUrl": "https://www.facebook.com/reel/123",
            "canonicalUrl": "https://www.facebook.com/reel/123",
            "localPath": "/tmp/video.mp4",
        }
        line = MODULE.VIDEO_RESULT_EVENT_PREFIX + json.dumps(
            {
                "event": "video-result",
                "source": "creator-one",
                "completed": 1,
                "total": 2,
                "video": video,
            }
        )
        progress = []
        recorder = MODULE.IncrementalVideoRecorder(
            "https://backend.example.com",
            "secret",
            "worker-01",
            "E-001",
            progress.append,
        )

        with (
            mock.patch.object(
                MODULE,
                "record_video",
                side_effect=[MODULE.BackendError("temporary"), None],
            ) as record_video,
            redirect_stderr(io.StringIO()),
        ):
            recorder.start()
            recorder.handle_line(line)
            recorder.finish([video])

        self.assertEqual(record_video.call_count, 2)
        self.assertIn(video["canonicalUrl"], recorder.recorded)
        self.assertEqual(recorder.stream_errors, {})
        self.assertEqual(progress, [48])

    def test_final_per_video_callback_failure_remains_fatal_for_lease_retry(self):
        video = {
            "status": "downloaded",
            "originalUrl": "https://www.facebook.com/reel/123",
            "canonicalUrl": "https://www.facebook.com/reel/123",
        }
        recorder = MODULE.IncrementalVideoRecorder(
            "https://backend.example.com",
            "secret",
            "worker-01",
            "E-001",
        )

        with (
            mock.patch.object(
                MODULE,
                "record_video",
                side_effect=MODULE.BackendError("backend unavailable"),
            ) as record_video,
            self.assertRaises(MODULE.BackendError),
        ):
            recorder.finish([video])

        record_video.assert_called_once()

    def test_filtered_duration_event_advances_progress_without_recording_video(self):
        line = MODULE.VIDEO_RESULT_EVENT_PREFIX + json.dumps(
            {
                "event": "video-result",
                "completed": 1,
                "total": 1,
                "video": {
                    "status": "filtered-duration",
                    "canonicalUrl": "https://www.facebook.com/reel/long",
                },
            }
        )
        progress = []
        recorder = MODULE.IncrementalVideoRecorder(
            "https://backend.example.com",
            "secret",
            "worker-01",
            "E-001",
            progress.append,
        )

        with mock.patch.object(MODULE, "record_video") as record_video:
            recorder.start()
            recorder.handle_line(line)
            recorder.finish([])

        record_video.assert_not_called()
        self.assertEqual(progress, [85])

    def test_upload_status_mapping_is_explicit(self):
        self.assertEqual(MODULE.normalized_upload_status("uploaded"), "UPLOADED")
        self.assertEqual(
            MODULE.normalized_upload_status("skipped-existing"),
            "SKIPPED_EXISTING",
        )
        self.assertEqual(MODULE.normalized_upload_status("conflict"), "R2_CONFLICT")
        self.assertEqual(MODULE.normalized_upload_status("unexpected"), "UPLOAD_FAILED")

    def test_successful_approved_upload_deletes_local_file_after_callback(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            local_video = root / "video.mp4"
            local_video.write_bytes(b"video")
            args = MODULE.build_parser().parse_args(
                ["--check", "--state-dir", str(root / "state")]
            )
            job = {
                "jobNo": "U-001",
                "localPath": str(local_video),
                "fileName": local_video.name,
                "fileSize": local_video.stat().st_size,
                "fileSha256": "a" * 64,
                "r2Prefix": "PH/Sports/202608/27",
            }

            def fake_run(command):
                result_path = Path(command[command.index("--result-json") + 1])
                result_path.write_text(
                    json.dumps(
                        {
                            "videos": [
                                {
                                    "status": "uploaded",
                                    "r2Bucket": "media",
                                    "r2ObjectKey": "PH/Sports/202608/27/video.mp4",
                                }
                            ]
                        }
                    ),
                    encoding="utf-8",
                )
                return 0, "uploaded\n"

            with (
                mock.patch.object(MODULE, "run_command", side_effect=fake_run),
                mock.patch.object(MODULE, "complete_upload") as complete_upload,
            ):
                result = MODULE.process_upload_job(
                    args,
                    "https://backend.example.com",
                    "secret",
                    "worker-01",
                    job,
                )

            complete_upload.assert_called_once()
            self.assertFalse(local_video.exists())
            self.assertEqual(result["localCleanup"]["status"], "deleted")

    def test_upload_callback_failure_keeps_local_file_for_retry(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            local_video = root / "video.mp4"
            local_video.write_bytes(b"video")
            args = MODULE.build_parser().parse_args(
                ["--check", "--state-dir", str(root / "state")]
            )
            job = {
                "jobNo": "U-001",
                "localPath": str(local_video),
                "fileName": local_video.name,
                "fileSize": local_video.stat().st_size,
                "fileSha256": "a" * 64,
                "r2Prefix": "PH/Sports/202608/27",
            }

            def fake_run(command):
                result_path = Path(command[command.index("--result-json") + 1])
                result_path.write_text(
                    json.dumps(
                        {
                            "videos": [
                                {
                                    "status": "uploaded",
                                    "r2ObjectKey": "PH/Sports/202608/27/video.mp4",
                                }
                            ]
                        }
                    ),
                    encoding="utf-8",
                )
                return 0, "uploaded\n"

            with (
                mock.patch.object(MODULE, "run_command", side_effect=fake_run),
                mock.patch.object(
                    MODULE,
                    "complete_upload",
                    side_effect=MODULE.BackendError("callback failed"),
                ),
                self.assertRaises(MODULE.BackendError),
            ):
                MODULE.process_upload_job(
                    args,
                    "https://backend.example.com",
                    "secret",
                    "worker-01",
                    job,
                )

            self.assertTrue(local_video.exists())
            journal = MODULE.upload_cleanup_journal_path(
                args.state_dir, job["jobNo"]
            )
            self.assertTrue(journal.is_file())

            with mock.patch.object(MODULE, "complete_upload") as complete_upload:
                replayed = MODULE.replay_upload_cleanup_journals(
                    args.state_dir,
                    "https://backend.example.com",
                    "secret",
                    "worker-01",
                )

            complete_upload.assert_called_once()
            self.assertEqual(replayed[0]["localCleanup"]["status"], "deleted")
            self.assertFalse(local_video.exists())
            self.assertFalse(journal.exists())

    def test_upload_complete_callback_enables_transient_retry(self):
        with mock.patch.object(MODULE, "api_call") as api_call:
            MODULE.complete_upload(
                "https://backend.example.com",
                "secret",
                "worker-01",
                "U-001",
                {
                    "status": "uploaded",
                    "r2Bucket": "media",
                    "r2ObjectKey": "PH/Sports/202608/27/video.mp4",
                },
            )

        self.assertTrue(api_call.call_args.kwargs["retry_transient"])

    def test_local_delete_claim_and_complete_use_worker_queue_contract(self):
        with mock.patch.object(
            MODULE,
            "api_call",
            return_value={"jobNo": "D-001", "videoNo": "V-001"},
        ) as api_call:
            claimed = MODULE.claim_local_delete(
                "https://backend.example.com", "secret", "worker-01"
            )
            MODULE.complete_local_delete(
                "https://backend.example.com",
                "secret",
                "worker-01",
                "D-001",
                "DELETED",
            )

        self.assertEqual(claimed["jobNo"], "D-001")
        self.assertEqual(
            api_call.call_args_list[0].args[3],
            "/api/internal/capture/local-deletes/claim",
        )
        self.assertEqual(api_call.call_args_list[0].args[4], {"workerId": "worker-01"})
        self.assertEqual(
            api_call.call_args_list[1].args[3],
            "/api/internal/capture/local-deletes/D-001/complete",
        )
        self.assertEqual(
            api_call.call_args_list[1].args[4],
            {
                "workerId": "worker-01",
                "status": "DELETED",
                "errorCode": None,
                "errorMessage": None,
            },
        )
        self.assertTrue(api_call.call_args_list[1].kwargs["retry_transient"])

    def test_local_delete_job_deletes_only_from_allowed_media_root(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state_dir = root / "state"
            video = state_dir / "E-001" / "video.mp4"
            video.parent.mkdir(parents=True)
            video.write_bytes(b"video")
            args = MODULE.build_parser().parse_args(
                ["--check", "--state-dir", str(state_dir)]
            )
            job = {
                "jobNo": "D-001",
                "videoNo": "V-001",
                "taskNo": "C-001",
                "workerId": "worker-01",
                "localPath": str(video),
            }

            with mock.patch.object(MODULE, "complete_local_delete") as complete:
                result = MODULE.process_local_delete_job(
                    args,
                    "https://backend.example.com",
                    "secret",
                    "worker-01",
                    job,
                )

            self.assertEqual(result["status"], "DELETED")
            self.assertFalse(video.exists())
            self.assertEqual(complete.call_args.args[4], "DELETED")

            outside = root / "outside.mp4"
            outside.write_bytes(b"video")
            job["jobNo"] = "D-002"
            job["localPath"] = str(outside)
            with mock.patch.object(MODULE, "complete_local_delete") as complete:
                result = MODULE.process_local_delete_job(
                    args,
                    "https://backend.example.com",
                    "secret",
                    "worker-01",
                    job,
                )

            self.assertEqual(result["status"], "DELETE_FAILED")
            self.assertEqual(result["errorCode"], "LOCAL_PATH_FORBIDDEN")
            self.assertTrue(outside.exists())
            self.assertEqual(complete.call_args.args[4], "DELETE_FAILED")

    @unittest.skipIf(os.name == "nt", "symlink creation may require elevated privileges")
    def test_local_delete_rejects_final_and_parent_symlinks(self):
        with tempfile.TemporaryDirectory() as temporary:
            state_dir = Path(temporary) / "state"
            state_dir.mkdir()
            target = state_dir / "target.mp4"
            target.write_bytes(b"target")
            final_link = state_dir / "linked.mp4"
            final_link.symlink_to(target)
            args = MODULE.build_parser().parse_args(
                ["--check", "--state-dir", str(state_dir)]
            )

            with mock.patch.object(MODULE, "complete_local_delete"):
                result = MODULE.process_local_delete_job(
                    args,
                    "https://backend.example.com",
                    "secret",
                    "worker-01",
                    {
                        "jobNo": "D-LINK-1",
                        "workerId": "worker-01",
                        "localPath": str(final_link),
                    },
                )

            self.assertEqual(result["status"], "DELETE_FAILED")
            self.assertEqual(result["errorCode"], "LOCAL_PATH_FORBIDDEN")
            self.assertTrue(final_link.is_symlink())
            self.assertTrue(target.exists())

            real_directory = state_dir / "real"
            real_directory.mkdir()
            nested_target = real_directory / "nested.mp4"
            nested_target.write_bytes(b"nested")
            parent_link = state_dir / "linked-directory"
            parent_link.symlink_to(real_directory, target_is_directory=True)
            with mock.patch.object(MODULE, "complete_local_delete"):
                result = MODULE.process_local_delete_job(
                    args,
                    "https://backend.example.com",
                    "secret",
                    "worker-01",
                    {
                        "jobNo": "D-LINK-2",
                        "workerId": "worker-01",
                        "localPath": str(parent_link / "nested.mp4"),
                    },
                )

            self.assertEqual(result["status"], "DELETE_FAILED")
            self.assertTrue(parent_link.is_symlink())
            self.assertTrue(nested_target.exists())

    def test_empty_media_path_environment_uses_safe_defaults(self):
        empty_paths = {
            "HM_INGEST_STATE_DIR": "  ",
            "FACEBOOK_FOLLOWED_OUTPUT": "",
            "FB_FOLLOWED_DESKTOP": "\t",
        }
        with mock.patch.dict(os.environ, empty_paths, clear=False):
            args = MODULE.build_parser().parse_args(["--check"])
            roots = MODULE.local_delete_roots(args.state_dir)

        self.assertEqual(args.state_dir, MODULE.DEFAULT_STATE_DIR)
        self.assertIn(MODULE.DEFAULT_STATE_DIR.expanduser().absolute(), roots)
        self.assertNotIn(Path.cwd().resolve(), roots)

    def test_local_delete_missing_file_reports_not_found(self):
        with tempfile.TemporaryDirectory() as temporary:
            state_dir = Path(temporary) / "state"
            args = MODULE.build_parser().parse_args(
                ["--check", "--state-dir", str(state_dir)]
            )
            with mock.patch.object(MODULE, "complete_local_delete") as complete:
                result = MODULE.process_local_delete_job(
                    args,
                    "https://backend.example.com",
                    "secret",
                    "worker-01",
                    {
                        "jobNo": "D-003",
                        "videoNo": "V-003",
                        "workerId": "worker-01",
                        "localPath": str(state_dir / "missing.mp4"),
                    },
                )

            self.assertEqual(result["status"], "NOT_FOUND")
            self.assertEqual(complete.call_args.args[4], "NOT_FOUND")

    def test_local_delete_drain_stops_after_one_failed_job(self):
        args = MODULE.build_parser().parse_args(["--check"])
        failed = {"jobNo": "D-004", "status": "DELETE_FAILED"}
        with (
            mock.patch.object(
                MODULE,
                "claim_local_delete",
                return_value={"jobNo": "D-004"},
            ) as claim,
            mock.patch.object(
                MODULE, "process_local_delete_job", return_value=failed
            ),
        ):
            results = MODULE.drain_local_delete_jobs(
                args,
                "https://backend.example.com",
                "secret",
                "worker-01",
            )

        self.assertEqual(results, [failed])
        claim.assert_called_once()

    def test_upload_only_reports_local_delete_work(self):
        args = MODULE.build_parser().parse_args(
            [
                "--execute",
                "--upload-only",
                "--backend",
                "https://backend.example.com",
                "--worker-id",
                "worker-01",
            ]
        )
        args.worker_token = "secret"
        deleted = [{"jobNo": "D-005", "status": "DELETED"}]
        drain_order = []

        def drain_local_deletes(*unused_args):
            drain_order.append("local-delete")
            return deleted

        def drain_uploads(*unused_args):
            drain_order.append("upload")
            return []

        with (
            mock.patch.object(MODULE, "register_media_endpoint"),
            mock.patch.object(
                MODULE, "drain_local_delete_jobs", side_effect=drain_local_deletes
            ),
            mock.patch.object(
                MODULE, "drain_upload_jobs", side_effect=drain_uploads
            ),
        ):
            exit_code, result = MODULE.execute_one(args)

        self.assertEqual(exit_code, 0)
        self.assertEqual(drain_order, ["local-delete", "upload"])
        self.assertEqual(result["status"], "local-deletes-completed")
        self.assertEqual(result["localDeletes"], deleted)

    def test_upload_only_delete_api_error_does_not_block_uploads(self):
        args = MODULE.build_parser().parse_args(
            [
                "--execute",
                "--upload-only",
                "--backend",
                "https://backend.example.com",
                "--worker-id",
                "worker-01",
            ]
        )
        args.worker_token = "secret"
        uploaded = [{"jobNo": "U-001", "status": "UPLOADED"}]
        with (
            mock.patch.object(MODULE, "register_media_endpoint"),
            mock.patch.object(
                MODULE,
                "drain_local_delete_jobs",
                side_effect=MODULE.BackendError("backend returned HTTP 404"),
            ),
            mock.patch.object(
                MODULE, "drain_upload_jobs", return_value=uploaded
            ) as drain_uploads,
            redirect_stderr(io.StringIO()),
        ):
            exit_code, result = MODULE.execute_one(args)

        self.assertEqual(exit_code, 0)
        drain_uploads.assert_called_once()
        self.assertEqual(result["uploads"], uploaded)
        self.assertEqual(result["status"], "queue-work-partial")
        self.assertIn("HTTP 404", result["localDeleteError"])

    def test_no_capture_delete_callback_conflict_does_not_block_uploads(self):
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
        uploaded = [{"jobNo": "U-002", "status": "UPLOADED"}]
        with (
            mock.patch.object(MODULE, "register_media_endpoint"),
            mock.patch.object(MODULE, "claim", return_value=None),
            mock.patch.object(
                MODULE,
                "drain_local_delete_jobs",
                side_effect=MODULE.BackendError("backend returned HTTP 409"),
            ),
            mock.patch.object(
                MODULE, "drain_upload_jobs", return_value=uploaded
            ) as drain_uploads,
            redirect_stderr(io.StringIO()),
        ):
            exit_code, result = MODULE.execute_one(args)

        self.assertEqual(exit_code, 0)
        drain_uploads.assert_called_once()
        self.assertEqual(result["uploads"], uploaded)
        self.assertEqual(result["status"], "queue-work-partial")
        self.assertIn("HTTP 409", result["localDeleteError"])

    def test_unsuccessful_upload_keeps_local_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            local_video = Path(temporary) / "video.mp4"
            local_video.write_bytes(b"video")

            cleanup = MODULE.cleanup_uploaded_local_file(
                {"localPath": str(local_video)},
                {"status": "conflict"},
            )

            self.assertEqual(cleanup["status"], "retained")
            self.assertTrue(local_video.exists())

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

    def test_transient_backend_failure_is_retried_for_safe_callback(self):
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return b'{"code":200,"message":"success","data":null}'

        with (
            mock.patch.object(
                MODULE.request,
                "urlopen",
                side_effect=[MODULE.error.URLError("temporary"), Response()],
            ) as urlopen,
            mock.patch.object(MODULE.time, "sleep") as sleep,
        ):
            MODULE.api_call(
                "https://live.example.com/hm",
                "secret",
                "POST",
                "/api/internal/capture/executions/E-001/heartbeat",
                {"workerId": "worker-01", "progress": 90},
                retry_transient=True,
            )

        self.assertEqual(urlopen.call_count, 2)
        sleep.assert_called_once_with(1)

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
        with (
            mock.patch.object(MODULE, "claim", return_value=None),
            mock.patch.object(MODULE, "claim_upload", return_value=None),
            mock.patch.object(MODULE, "claim_local_delete", return_value=None),
        ):
            exit_code, result = MODULE.execute_one(args)
        self.assertEqual(exit_code, 0)
        self.assertEqual(result["status"], "no-work")
        self.assertEqual(result["localDeletes"], [])

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
        download_run_kwargs = {}

        def fake_run(command, **kwargs):
            commands.append(command)
            result_path = Path(command[command.index("--result-json") + 1])
            if "facebook_followed_video_download.py" in command[1]:
                download_run_kwargs.update(kwargs)
                kwargs["on_line"](
                    MODULE.VIDEO_RESULT_EVENT_PREFIX
                    + json.dumps(
                        {
                            "event": "video-result",
                            "source": "C-001",
                            "completed": 1,
                            "total": 1,
                            "video": download_video,
                        }
                    )
                )
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
            mock.patch.object(MODULE, "claim_upload", return_value=None),
            mock.patch.object(MODULE, "claim_local_delete", return_value=None),
        ):
            exit_code, result = MODULE.execute_one(args)

        self.assertEqual(exit_code, 0)
        self.assertEqual(result["status"], "COMPLETED")
        self.assertEqual(record.call_count, 1)
        self.assertEqual(complete.call_args.args[4], "COMPLETED")
        download_command = commands[0]
        self.assertEqual(
            download_command[download_command.index("--initial-count") + 1],
            "10",
        )
        self.assertEqual(
            download_command[download_command.index("--max-duration-seconds") + 1],
            "1200",
        )
        self.assertEqual(
            download_run_kwargs["env"][MODULE.VIDEO_RESULT_EVENTS_ENV],
            "1",
        )

    def test_unreconciled_stream_callback_leaves_execution_for_lease_retry(self):
        with tempfile.TemporaryDirectory() as temporary:
            args = MODULE.build_parser().parse_args(
                [
                    "--execute",
                    "--backend",
                    "https://backend.example.com",
                    "--worker-id",
                    "worker-01",
                    "--state-dir",
                    temporary,
                ]
            )
            args.worker_token = "secret"
            video = {
                "source": "creator-one",
                "platformVideoId": "123",
                "originalUrl": "https://www.facebook.com/reel/123",
                "canonicalUrl": "https://www.facebook.com/reel/123",
                "localPath": "/tmp/video.mp4",
                "fileName": "video.mp4",
                "status": "downloaded",
            }

            def fake_run(command, **kwargs):
                kwargs["on_line"](
                    MODULE.VIDEO_RESULT_EVENT_PREFIX
                    + json.dumps(
                        {
                            "event": "video-result",
                            "completed": 1,
                            "total": 1,
                            "video": video,
                        }
                    )
                )
                result_path = Path(command[command.index("--result-json") + 1])
                result_path.write_text(
                    json.dumps(
                        {
                            "status": "completed",
                            "sources": [
                                {"name": "creator-one", "videos": [video]}
                            ],
                        }
                    ),
                    encoding="utf-8",
                )
                return 0, "ok\n"

            with (
                mock.patch.object(
                    MODULE,
                    "claim",
                    return_value={
                        "executionId": "E-STREAM-FAIL",
                        "sourceName": "creator-one",
                        "sourceUrl": "https://www.facebook.com/example/reels/",
                    },
                ),
                mock.patch.object(MODULE, "heartbeat"),
                mock.patch.object(MODULE, "run_command", side_effect=fake_run),
                mock.patch.object(
                    MODULE,
                    "record_video",
                    side_effect=MODULE.BackendError("backend unavailable"),
                ),
                mock.patch.object(MODULE, "complete") as complete,
                redirect_stderr(io.StringIO()),
            ):
                exit_code, result = MODULE.execute_one(args)

            self.assertEqual(exit_code, 1)
            self.assertEqual(result["retry"], "lease")
            complete.assert_not_called()

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

        def fake_run(command, **kwargs):
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
            mock.patch.object(MODULE, "claim_upload", return_value=None),
            mock.patch.object(MODULE, "claim_local_delete", return_value=None),
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
