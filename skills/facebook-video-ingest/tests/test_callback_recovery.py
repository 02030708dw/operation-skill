import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SPEC = importlib.util.spec_from_file_location(
    "ingest_callback_recovery",
    Path(__file__).resolve().parents[1] / "scripts" / "facebook_video_ingest.py",
)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class Response:
    def __init__(self, code=200):
        self.code = code

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return json.dumps({"code": self.code, "data": None}).encode()


def rejected(status):
    return MODULE.error.HTTPError(
        "https://backend.example.com", status, "rejected", {},
        io.BytesIO('title 不能超过 300 个字符'.encode()),
    )


class CallbackRecoveryTests(unittest.TestCase):
    def execute(self, respond, videos=None):
        """Exercise real registration/completion requests after a durable download."""
        if videos is None:
            videos = [{
                "originalUrl": "https://www.facebook.com/reel/123",
                "fileName": "video.mp4", "status": "downloaded",
            }]
        calls = []

        def urlopen(outgoing, timeout):
            payload = json.loads(outgoing.data)
            calls.append((outgoing.full_url, payload))
            return respond(outgoing.full_url, payload)

        with tempfile.TemporaryDirectory() as temporary:
            args = MODULE.build_parser().parse_args([
                "--execute", "--backend", "https://backend.example.com",
                "--worker-id", "worker-01", "--state-dir", temporary,
            ])
            args.worker_token = "secret"
            with (
                mock.patch.object(MODULE, "download_runtime_check", return_value={"ready": True}),
                mock.patch.object(MODULE, "claim", return_value={"executionId": "E-001"}),
                mock.patch.object(MODULE, "heartbeat"),
                mock.patch.object(MODULE, "HeartbeatPump"),
                mock.patch.object(MODULE, "reusable_download_result", return_value={
                    "status": "completed", "sources": [{"videos": videos}],
                }),
                mock.patch.object(MODULE, "drain_local_delete_jobs", return_value=[]),
                mock.patch.object(MODULE, "drain_upload_jobs", return_value=[]),
                mock.patch.object(MODULE.request, "urlopen", side_effect=urlopen),
                mock.patch.object(MODULE.time, "sleep"),
            ):
                exit_code, result = MODULE.execute_one(args)
        return exit_code, result, calls

    def test_long_unicode_titles_complete_and_keep_original_metadata(self):
        titles = ["短标题", "a" * 300, "中" * 301, "😀" * 151, "a" * 299 + "😀"]
        videos = [{
            "originalUrl": f"https://www.facebook.com/reel/{index}",
            "fileName": "video.mp4", "title": title, "status": "downloaded",
        } for index, title in enumerate(titles)]

        def respond(url, payload):
            if url.endswith("/videos") and len(payload["title"].encode("utf-16-le")) > 600:
                raise rejected(400)
            return Response()

        exit_code, result, calls = self.execute(respond, videos)
        self.assertEqual((exit_code, result["status"]), (0, "COMPLETED"))
        records = [payload for url, payload in calls if url.endswith("/videos")]
        self.assertEqual([item["title"] for item in records], [
            "短标题", "a" * 300, "中" * 300, "😀" * 150, "a" * 299,
        ])
        self.assertEqual([json.loads(item["metadataJson"])["title"] for item in records], titles)
        self.assertEqual(calls[-1][1]["status"], "COMPLETED")

    def test_permanent_video_rejection_reports_failed_instead_of_waiting_for_lease(self):
        for status in (400, 422):
            with self.subTest(status=status):
                def respond(url, payload):
                    if url.endswith("/videos"):
                        raise rejected(status)
                    return Response()

                exit_code, result, calls = self.execute(respond)
                self.assertEqual((exit_code, result["status"]), (1, "FAILED"))
                self.assertNotIn("retry", result)
                self.assertNotIn("callbackError", result)
                self.assertEqual(len(calls), 2)
                self.assertTrue(calls[-1][0].endswith("/complete"))
                self.assertEqual(calls[-1][1]["status"], "FAILED")
                self.assertEqual(calls[-1][1]["errorCode"], "BACKEND_REQUEST_REJECTED")

    def test_business_envelope_validation_error_also_reports_failed(self):
        exit_code, result, calls = self.execute(
            lambda url, payload: Response(400 if url.endswith("/videos") else 200)
        )
        self.assertEqual((exit_code, result["status"]), (1, "FAILED"))
        self.assertNotIn("retry", result)
        self.assertEqual(calls[-1][1]["errorCode"], "BACKEND_REQUEST_REJECTED")

    def test_temporary_video_rejection_keeps_lease_recovery(self):
        for status in (408, 425, 429, 500, 503):
            with self.subTest(status=status):
                def respond(url, payload):
                    raise rejected(status)

                exit_code, result, calls = self.execute(respond)
                self.assertEqual((exit_code, result["retry"]), (1, "lease"))
                self.assertEqual(len(calls), MODULE.TRANSIENT_BACKEND_ATTEMPTS)
                self.assertTrue(all(url.endswith("/videos") for url, _ in calls))

    def test_lost_completion_response_never_overwrites_possible_success(self):
        def respond(url, payload):
            if url.endswith("/complete"):
                raise MODULE.error.URLError("response lost after commit")
            return Response()

        exit_code, result, calls = self.execute(respond)
        self.assertEqual((exit_code, result["retry"]), (1, "lease"))
        completions = [payload for url, payload in calls if url.endswith("/complete")]
        self.assertEqual(len(completions), MODULE.TRANSIENT_BACKEND_ATTEMPTS)
        self.assertTrue(all(payload["status"] == "COMPLETED" for payload in completions))

    def test_explicit_completion_rejection_can_report_failed(self):
        def respond(url, payload):
            if url.endswith("/complete") and payload["status"] == "COMPLETED":
                raise rejected(400)
            return Response()

        exit_code, result, calls = self.execute(respond)
        self.assertEqual((exit_code, result["status"]), (1, "FAILED"))
        self.assertNotIn("retry", result)
        completions = [payload for url, payload in calls if url.endswith("/complete")]
        self.assertEqual([payload["status"] for payload in completions], ["COMPLETED", "FAILED"])
        self.assertEqual(completions[-1]["errorCode"], "BACKEND_REQUEST_REJECTED")

    def test_failed_status_delivery_outage_is_reported_for_recovery(self):
        def respond(url, payload):
            if url.endswith("/videos"):
                raise rejected(400)
            raise MODULE.error.URLError("network offline")

        exit_code, result, calls = self.execute(respond)
        self.assertEqual((exit_code, result["retry"]), (1, "lease"))
        self.assertIn("network offline", result["callbackError"])
        self.assertEqual(calls[-1][1]["status"], "FAILED")

    def test_diagnostics_fit_backend_limits_and_keep_original_error(self):
        original_error = "😀" * 600
        video = {"originalUrl": "https://www.facebook.com/reel/123", "error": original_error}
        with mock.patch.object(MODULE, "api_call") as api_call:
            MODULE.record_video("https://backend.example.com", "secret", "worker-01", "E-001",
                                video, download_status="DOWNLOAD_FAILED", upload_status="PENDING")
            record = api_call.call_args.args[4]
            self.assertEqual(record["errorMessage"], "😀" * 250)
            self.assertEqual(json.loads(record["metadataJson"])["error"], original_error)
            MODULE.complete("https://backend.example.com", "secret", "worker-01", "E-001",
                            "FAILED", {"error": original_error}, "😀" * 500_001 + "end",
                            "BACKEND_REQUEST_REJECTED", original_error)
            completion = api_call.call_args.args[4]
        self.assertEqual(completion["errorMessage"], "😀" * 250)
        self.assertLessEqual(len(completion["rawOutput"].encode("utf-16-le")), 2_000_000)
        self.assertTrue(completion["rawOutput"].endswith("end"))
        self.assertEqual(json.loads(completion["resultJson"])["error"], original_error)


if __name__ == "__main__":
    unittest.main()
