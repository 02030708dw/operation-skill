#!/usr/bin/env python3
"""Claim one HM capture execution, download Facebook videos, upload them to R2, and report results."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import IO, Any, Callable
from urllib import error, request


SKILL_NAME = "facebook-video-ingest"
SKILL_VERSION = "1.2.3"
WORKER_USER_AGENT = "HM-Hermes-Worker/1.0"
TRANSIENT_HTTP_STATUSES = {408, 425, 429, 500, 502, 503, 504}
TRANSIENT_BACKEND_ATTEMPTS = 6
VIDEO_RESULT_EVENT_PREFIX = "__HM_VIDEO_RESULT__:"
VIDEO_RESULT_EVENTS_ENV = "HM_VIDEO_RESULT_EVENTS"
NON_ACTIONABLE_VIDEO_STATUSES = {"filtered-duration", "archived-existing"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v", ".webm", ".mkv"}
MAX_LOCAL_DELETE_JOBS_PER_POLL = 100
SKILL_DIR = Path(__file__).resolve().parent.parent
SKILLS_DIR = SKILL_DIR.parent
DOWNLOAD_SCRIPT = (
    SKILLS_DIR
    / "facebook-followed-video-download"
    / "scripts"
    / "facebook_followed_video_download.py"
)
R2_SCRIPT = (
    SKILLS_DIR
    / "cloudflare-r2-video-upload"
    / "scripts"
    / "cloudflare_r2_video_upload.py"
)


def infer_hermes_home() -> Path:
    configured = os.getenv("HERMES_HOME")
    if configured:
        return Path(configured).expanduser()
    if SKILLS_DIR.name.lower() == "skills":
        return SKILLS_DIR.parent
    return Path.home() / ".hermes"


DEFAULT_STATE_DIR = infer_hermes_home() / SKILL_NAME / "executions"


class PipelineError(RuntimeError):
    def __init__(self, message: str, error_code: str = "PIPELINE_ERROR") -> None:
        super().__init__(message)
        self.error_code = error_code


class BackendError(PipelineError):
    def __init__(self, message: str, *, http_status: int | None = None) -> None:
        # An explicit client rejection was not committed. Network failures and
        # server errors can have an uncertain outcome and must keep lease recovery.
        self.retryable = not (
            http_status is not None
            and 400 <= http_status < 500
            and http_status not in TRANSIENT_HTTP_STATUSES
        )
        self.http_status = http_status
        super().__init__(
            message,
            "PIPELINE_ERROR" if self.retryable else "BACKEND_REQUEST_REJECTED",
        )


def bounded_backend_text(value: str | None, limit: int, *, tail: bool = False) -> str | None:
    """Match Java String.length limits without splitting a Unicode surrogate pair."""
    if value is None:
        return None
    encoded = value.encode("utf-16-le")
    bounded = encoded[-limit * 2:] if tail else encoded[:limit * 2]
    return bounded.decode("utf-16-le", errors="ignore")


def environment_path(name: str, fallback: Path) -> Path:
    """Read a path setting without treating an empty value as the current directory."""
    configured = os.getenv(name, "").strip()
    return Path(configured).expanduser() if configured else fallback


def absolute_path_without_resolving_links(path: Path) -> Path:
    """Normalize an absolute path while preserving symlink components for checks."""
    return Path(os.path.abspath(os.fspath(path.expanduser())))


def load_env_file(path: Path) -> None:
    """Load missing Worker settings from Hermes' private environment file."""
    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, separator, value = line.partition("=")
        key = key.strip()
        if not separator or not key or not key.replace("_", "A").isalnum():
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        os.environ.setdefault(key, value)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run one backend-managed Facebook download and Cloudflare R2 ingest execution"
    )
    parser.add_argument("--backend", default=os.getenv("HM_BACKEND_URL", ""))
    parser.add_argument("--worker-id", default=os.getenv("HM_WORKER_ID", ""))
    parser.add_argument(
        "--media-base-url",
        default=os.getenv("HM_CAPTURE_MEDIA_BASE_URL", ""),
        help="private Hermes media base URL registered for cross-computer review",
    )
    parser.add_argument(
        "--task-no",
        default=os.getenv("HM_CAPTURE_TASK_NO", ""),
        help="claim only the queued execution for this HM task number",
    )
    parser.add_argument(
        "--execution-no",
        default=os.getenv("HM_CAPTURE_EXECUTION_NO", ""),
        help="claim only this exact queued HM execution number",
    )
    parser.set_defaults(worker_token=os.getenv("HM_WORKER_TOKEN", ""))
    parser.add_argument(
        "--initial-count",
        "--count",
        dest="count",
        type=positive_int,
        default=10,
        help="first daily execution limit per source (default: 10)",
    )
    parser.add_argument(
        "--r2-prefix",
        default=os.getenv("HM_R2_PREFIX", ""),
        help="legacy fallback; backend job r2Prefix takes precedence",
    )
    parser.add_argument("--download-output", type=Path)
    parser.add_argument(
        "--state-dir",
        type=Path,
        default=environment_path("HM_INGEST_STATE_DIR", DEFAULT_STATE_DIR),
    )
    parser.add_argument(
        "--heartbeat-seconds",
        type=positive_int,
        default=positive_int(os.getenv("HM_WORKER_HEARTBEAT_SECONDS", "30")),
    )
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--check", action="store_true")
    action.add_argument("--execute", action="store_true", help="claim and run at most one job")
    action.add_argument("--watch", action="store_true", help="continuously poll and run jobs")
    parser.add_argument(
        "--poll-seconds",
        type=positive_int,
        default=positive_int(os.getenv("HM_WORKER_POLL_SECONDS", "15")),
    )
    parser.add_argument(
        "--wait-for-work-seconds",
        type=non_negative_int,
        default=0,
        help="wait briefly for the targeted backend scheduler to enqueue work",
    )
    parser.add_argument(
        "--upload-only",
        action="store_true",
        help="skip capture claiming and drain approved upload and local-delete jobs",
    )
    parser.add_argument("--json", action="store_true")
    return parser


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than 0")
    return parsed


def non_negative_int(value: str) -> int:
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be zero or greater")
    return parsed


def normalize_backend(value: str) -> str:
    normalized = value.strip().rstrip("/")
    if not normalized.startswith(("http://", "https://")):
        raise PipelineError("HM_BACKEND_URL must start with http:// or https://")
    return normalized


def watch_lock_path(state_dir: Path) -> Path:
    return state_dir.expanduser().resolve().parent / "watch.lock"


def acquire_watch_lock(path: Path) -> IO[str] | None:
    """Allow only one customer-side polling Worker per local Hermes profile."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+", encoding="utf-8")
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            if path.stat().st_size == 0:
                handle.write("0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (OSError, BlockingIOError):
        handle.close()
        return None
    return handle


def api_call(
    backend: str,
    token: str,
    method: str,
    path: str,
    payload: dict[str, Any],
    *,
    worker_id: str | None = None,
    retry_transient: bool = False,
) -> Any:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": WORKER_USER_AGENT,
        "X-HM-Worker-Token": token,
    }
    if worker_id:
        headers["X-HM-Worker-Id"] = worker_id
    attempts = TRANSIENT_BACKEND_ATTEMPTS if retry_transient else 1
    for attempt in range(attempts):
        outgoing = request.Request(
            f"{backend}{path}", data=body, headers=headers, method=method
        )
        try:
            with request.urlopen(outgoing, timeout=30) as response:
                parsed = json.loads(response.read().decode("utf-8"))
            break
        except error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:1000]
            if exc.code not in TRANSIENT_HTTP_STATUSES or attempt + 1 >= attempts:
                raise BackendError(
                    f"backend returned HTTP {exc.code}: {detail}",
                    http_status=exc.code,
                ) from exc
        except (error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            if attempt + 1 >= attempts:
                raise BackendError(f"backend request failed: {exc}") from exc
        time.sleep(min(2 ** attempt, 8))
    if not isinstance(parsed, dict) or parsed.get("code") != 200:
        code = parsed.get("code") if isinstance(parsed, dict) else None
        raise BackendError(
            f"backend rejected request: {parsed}",
            http_status=code if isinstance(code, int) else None,
        )
    return parsed.get("data")


def claim(
    backend: str,
    token: str,
    worker_id: str,
    task_no: str = "",
    execution_no: str = "",
) -> dict[str, Any] | None:
    payload = {"workerId": worker_id}
    if task_no.strip():
        payload["taskNo"] = task_no.strip().upper()
    if execution_no.strip():
        payload["executionNo"] = execution_no.strip().upper()
    return api_call(
        backend,
        token,
        "POST",
        "/api/internal/capture/executions/claim",
        payload,
    )


def register_media_endpoint(
    backend: str, token: str, worker_id: str, media_base_url: str
) -> None:
    normalized = media_base_url.strip().rstrip("/")
    if not normalized:
        return
    api_call(
        backend,
        token,
        "POST",
        "/api/internal/capture/workers/register",
        {"workerId": worker_id, "mediaBaseUrl": normalized},
    )


def claim_upload(
    backend: str, token: str, worker_id: str, task_no: str = ""
) -> dict[str, Any] | None:
    payload = {"workerId": worker_id}
    if task_no.strip():
        payload["taskNo"] = task_no.strip().upper()
    return api_call(
        backend, token, "POST", "/api/internal/capture/uploads/claim", payload
    )


def complete_upload(
    backend: str,
    token: str,
    worker_id: str,
    job_no: str,
    video: dict[str, Any],
) -> None:
    upload_status = normalized_upload_status(video.get("status"))
    api_call(
        backend,
        token,
        "POST",
        f"/api/internal/capture/uploads/{job_no}/complete",
        {
            "workerId": worker_id,
            "status": upload_status,
            "r2Bucket": video.get("r2Bucket"),
            "r2ObjectKey": video.get("r2ObjectKey"),
            "r2Url": video.get("r2Url"),
            "errorCode": status_error_code("DOWNLOADED", upload_status),
            "errorMessage": video.get("error"),
        },
        retry_transient=True,
    )


def claim_local_delete(
    backend: str, token: str, worker_id: str
) -> dict[str, Any] | None:
    return api_call(
        backend,
        token,
        "POST",
        "/api/internal/capture/local-deletes/claim",
        {"workerId": worker_id},
    )


def complete_local_delete(
    backend: str,
    token: str,
    worker_id: str,
    job_no: str,
    status: str,
    error_code: str | None = None,
    error_message: str | None = None,
) -> None:
    if status not in {"DELETED", "NOT_FOUND", "DELETE_FAILED"}:
        raise PipelineError(f"unsupported local-delete status: {status}")
    api_call(
        backend,
        token,
        "POST",
        f"/api/internal/capture/local-deletes/{job_no}/complete",
        {
            "workerId": worker_id,
            "status": status,
            "errorCode": error_code,
            "errorMessage": error_message,
        },
        retry_transient=True,
    )


def heartbeat(backend: str, token: str, worker_id: str, execution_id: str, progress: int) -> None:
    api_call(
        backend,
        token,
        "POST",
        f"/api/internal/capture/executions/{execution_id}/heartbeat",
        {"workerId": worker_id, "progress": progress},
        retry_transient=True,
    )


class HeartbeatPump:
    """Keep a claimed execution alive while child download/upload processes run."""

    def __init__(
        self,
        backend: str,
        token: str,
        worker_id: str,
        execution_id: str,
        interval: int,
    ) -> None:
        self.backend = backend
        self.token = token
        self.worker_id = worker_id
        self.execution_id = execution_id
        self.interval = interval
        self.progress = 5
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._run,
            name=f"hm-heartbeat-{self.execution_id}",
            daemon=True,
        )
        self._thread.start()

    def update(self, progress: int) -> None:
        with self._lock:
            self.progress = progress

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=min(5, self.interval))

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            with self._lock:
                progress = self.progress
            try:
                heartbeat(
                    self.backend,
                    self.token,
                    self.worker_id,
                    self.execution_id,
                    progress,
                )
            except PipelineError as exc:
                print(f"Heartbeat warning: {exc}", file=sys.stderr, flush=True)


def record_video(
    backend: str,
    token: str,
    worker_id: str,
    execution_id: str,
    video: dict[str, Any],
    *,
    download_status: str,
    upload_status: str,
) -> None:
    original_url = video.get("originalUrl") or video.get("canonicalUrl")
    if not original_url:
        raise PipelineError("video result is missing originalUrl")
    payload = {
        "platformVideoId": video.get("platformVideoId"),
        "sourceName": video.get("source"),
        "title": bounded_backend_text(
            video.get("title") or video.get("fileName") or video.get("platformVideoId"),
            300,
        ),
        "originalUrl": original_url,
        "canonicalUrl": video.get("canonicalUrl") or original_url,
        "localPath": video.get("localPath"),
        "fileName": video.get("fileName"),
        "fileSize": video.get("fileSize"),
        "fileSha256": video.get("sha256"),
        "durationSeconds": video.get("durationSeconds"),
        "publishedAt": video.get("publishedAt"),
        "downloadStatus": download_status,
        "uploadStatus": upload_status,
        "r2Bucket": video.get("r2Bucket"),
        "r2ObjectKey": video.get("r2ObjectKey"),
        "r2Url": video.get("r2Url"),
        "errorCode": status_error_code(download_status, upload_status),
        "errorMessage": bounded_backend_text(video.get("error"), 500),
        "metadataJson": json.dumps(video, ensure_ascii=False),
    }
    api_call(
        backend,
        token,
        "POST",
        f"/api/internal/capture/executions/{execution_id}/videos",
        payload,
        worker_id=worker_id,
        retry_transient=True,
    )


def status_error_code(download_status: str, upload_status: str) -> str | None:
    if download_status == "DOWNLOAD_FAILED":
        return "DOWNLOAD_FAILED"
    if upload_status in {"R2_CONFLICT", "UPLOAD_FAILED"}:
        return upload_status
    return None


def parse_download_video_event(line: str) -> dict[str, Any] | None:
    """Parse one opt-in downloader event without treating normal logs as data."""
    value = line.strip()
    if not value.startswith(VIDEO_RESULT_EVENT_PREFIX):
        return None
    try:
        event = json.loads(value[len(VIDEO_RESULT_EVENT_PREFIX) :])
    except json.JSONDecodeError:
        return None
    if not isinstance(event, dict) or event.get("event") != "video-result":
        return None
    video = event.get("video")
    if not isinstance(video, dict):
        return None
    normalized = dict(video)
    if not normalized.get("source") and event.get("source"):
        normalized["source"] = event["source"]
    return {
        "video": normalized,
        "completed": event.get("completed"),
        "total": event.get("total"),
    }


def video_result_identity(video: dict[str, Any]) -> str:
    """Use the same stable URL identity that HM uses for idempotent upserts."""
    identity = str(
        video.get("canonicalUrl") or video.get("originalUrl") or ""
    ).strip()
    if not identity:
        raise PipelineError("video result is missing canonicalUrl/originalUrl")
    return identity


def capture_download_status(video: dict[str, Any]) -> str | None:
    status = str(video.get("status") or "").strip().lower()
    if status in NON_ACTIONABLE_VIDEO_STATUSES:
        return None
    return "DOWNLOADED" if status == "downloaded" else "DOWNLOAD_FAILED"


class IncrementalVideoRecorder:
    """Deliver terminal per-video results while the downloader is still running.

    A dedicated thread keeps backend retries from blocking the downloader's
    stdout pipe. ``finish`` joins that thread and reconciles the authoritative
    final manifest, retrying any event callback that did not reach HM.
    """

    _STOP = object()

    def __init__(
        self,
        backend: str,
        token: str,
        worker_id: str,
        execution_id: str,
        progress_callback: Callable[[int], None] | None = None,
    ) -> None:
        self.backend = backend
        self.token = token
        self.worker_id = worker_id
        self.execution_id = execution_id
        self.progress_callback = progress_callback
        self.recorded: set[str] = set()
        self.stream_errors: dict[str, str] = {}
        self._queue: queue.Queue[dict[str, Any] | object] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._stopped = False

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run,
            name=f"hm-video-results-{self.execution_id}",
            daemon=True,
        )
        self._thread.start()

    def handle_line(self, line: str) -> None:
        event = parse_download_video_event(line)
        if event is None or self._stopped:
            return
        video = event["video"]
        if capture_download_status(video) is not None:
            self._queue.put(video)
        try:
            completed = int(event.get("completed") or 0)
            total = int(event.get("total") or 0)
        except (TypeError, ValueError):
            return
        if self.progress_callback and completed > 0 and total > 0:
            progress = min(85, 10 + round(75 * min(completed, total) / total))
            self.progress_callback(progress)

    def finish(self, videos: list[dict[str, Any]]) -> None:
        """Drain streamed callbacks, then retry every missing final result."""
        self.stop()
        for video in videos:
            if capture_download_status(video) is None:
                continue
            identity = video_result_identity(video)
            if identity in self.recorded:
                continue
            self._record(video)

    def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        if self._thread is not None:
            self._queue.put(self._STOP)
            self._thread.join()

    def _run(self) -> None:
        while True:
            queued = self._queue.get()
            try:
                if queued is self._STOP:
                    return
                assert isinstance(queued, dict)
                try:
                    identity = video_result_identity(queued)
                    if identity in self.recorded:
                        continue
                    self._record(queued)
                except Exception as exc:
                    # The final manifest is authoritative and retries this
                    # exact idempotent upsert before execution completion.
                    identity = str(
                        queued.get("canonicalUrl")
                        or queued.get("originalUrl")
                        or queued.get("platformVideoId")
                        or "unknown"
                    )
                    self.stream_errors[identity] = str(exc)
                    print(
                        f"Per-video callback warning ({identity}): {exc}",
                        file=sys.stderr,
                        flush=True,
                    )
            finally:
                self._queue.task_done()

    def _record(self, video: dict[str, Any]) -> None:
        identity = video_result_identity(video)
        download_status = capture_download_status(video)
        if download_status is None:
            return
        record_video(
            self.backend,
            self.token,
            self.worker_id,
            self.execution_id,
            video,
            download_status=download_status,
            upload_status="PENDING",
        )
        self.recorded.add(identity)
        self.stream_errors.pop(identity, None)


def complete(
    backend: str,
    token: str,
    worker_id: str,
    execution_id: str,
    status: str,
    result: dict[str, Any],
    raw_output: str,
    error_code: str | None = None,
    error_message: str | None = None,
) -> None:
    api_call(
        backend,
        token,
        "POST",
        f"/api/internal/capture/executions/{execution_id}/complete",
        {
            "workerId": worker_id,
            "status": status,
            "progress": 100 if status == "COMPLETED" else 95,
            "resultJson": json.dumps(result, ensure_ascii=False),
            "rawOutput": bounded_backend_text(raw_output, 1_000_000, tail=True),
            "errorCode": error_code,
            "errorMessage": bounded_backend_text(error_message, 500),
        },
        retry_transient=True,
    )


def run_command(
    command: list[str],
    *,
    env: dict[str, str] | None = None,
    on_line: Callable[[str], None] | None = None,
) -> tuple[int, str]:
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
    )
    lines: list[str] = []
    assert process.stdout is not None
    for line in process.stdout:
        if on_line is not None:
            on_line(line)
        if line.startswith(VIDEO_RESULT_EVENT_PREFIX):
            continue
        print(line, end="", flush=True)
        lines.append(line)
    process.stdout.close()
    return process.wait(), "".join(lines)


def download_runtime_check() -> dict[str, Any]:
    if not DOWNLOAD_SCRIPT.is_file():
        raise PipelineError(
            f"download Skill entry point is missing: {DOWNLOAD_SCRIPT}",
            "DOWNLOAD_RUNTIME_NOT_READY",
        )
    completed = subprocess.run(
        [sys.executable, str(DOWNLOAD_SCRIPT), "--runtime-check"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        timeout=45,
    )
    try:
        status = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        detail = (completed.stderr or completed.stdout or "no output").strip()[-1000:]
        raise PipelineError(
            f"download Skill runtime check returned invalid output: {detail}",
            "DOWNLOAD_RUNTIME_NOT_READY",
        ) from exc
    if completed.returncode != 0 or not status.get("runtimeReady"):
        missing = [
            name
            for name, ready in (
                ("Node.js 12.22+", status.get("nodeSupported")),
                ("download engine syntax", status.get("engineSyntaxOk")),
                ("Node ws module", status.get("wsModule")),
                ("Chrome", status.get("chromeRunnable")),
                ("yt-dlp", status.get("ytDlpRunnable")),
            )
            if not ready
        ]
        raise PipelineError(
            "download Skill runtime is not ready: " + ", ".join(missing or ["unknown failure"]),
            "DOWNLOAD_RUNTIME_NOT_READY",
        )
    return status


def manifest_videos(payload: dict[str, Any]) -> list[dict[str, Any]]:
    videos: list[dict[str, Any]] = []
    for source in payload.get("sources", []):
        for video in source.get("videos", []):
            item = dict(video)
            item.setdefault("source", source.get("name"))
            videos.append(item)
    return videos


def manifest_error_code(payload: dict[str, Any]) -> str | None:
    top_level = str(payload.get("errorCode") or "").strip()
    if top_level:
        return top_level[:100]
    for source in payload.get("sources", []):
        source_code = str(source.get("errorCode") or "").strip()
        if source_code:
            return source_code[:100]
    return None


def normalized_upload_status(value: object) -> str:
    mapping = {
        "uploaded": "UPLOADED",
        "skipped-existing": "SKIPPED_EXISTING",
        "conflict": "R2_CONFLICT",
        "failed": "UPLOAD_FAILED",
        "ready": "PENDING",
    }
    return mapping.get(str(value or "").lower(), "UPLOAD_FAILED")


def state_segment(value: object) -> str:
    normalized = re.sub(r"[^A-Za-z0-9_-]", "_", str(value or "").strip())[:80]
    if not normalized:
        raise PipelineError("backend executionId is invalid")
    return normalized


def job_r2_prefix(job: dict[str, Any], fallback: str = "") -> str:
    prefix = str(job.get("r2Prefix") or fallback or "").strip().strip("/")
    parts = prefix.split("/")
    if len(parts) != 4 or any(not part or part in {".", ".."} for part in parts):
        raise PipelineError(
            "backend job r2Prefix must use REGION/Category/yyyyMM/dd"
        )
    return prefix


def reusable_download_result(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or payload.get("status") not in {"completed", "partial"}:
        return None
    for video in manifest_videos(payload):
        if video.get("status") != "downloaded":
            continue
        local_value = video.get("localPath")
        if not local_value:
            return None
        local_path = Path(str(local_value)).expanduser()
        if not local_path.is_file():
            return None
        expected_size = video.get("fileSize")
        if expected_size is not None and local_path.stat().st_size != int(expected_size):
            return None
    return payload


def cleanup_uploaded_local_file(
    job: dict[str, Any], upload_video: dict[str, Any]
) -> dict[str, Any]:
    """Delete the exact source file only after a verified successful upload."""
    upload_status = normalized_upload_status(str(upload_video.get("status") or ""))
    if upload_status not in {"UPLOADED", "SKIPPED_EXISTING"}:
        return {"status": "retained", "reason": "upload-not-successful"}
    local_value = job.get("localPath")
    if not local_value:
        return {"status": "already-missing"}
    local_path = Path(str(local_value)).expanduser().resolve()
    if local_path.suffix.lower() not in {".mp4", ".mov", ".m4v", ".webm", ".mkv"}:
        return {"status": "failed", "error": "local path is not a supported video"}
    try:
        local_path.unlink()
        return {"status": "deleted", "fileName": local_path.name}
    except FileNotFoundError:
        return {"status": "already-missing", "fileName": local_path.name}
    except OSError as exc:
        return {"status": "failed", "fileName": local_path.name, "error": str(exc)}


def upload_cleanup_journal_path(state_dir: Path, job_no: str) -> Path:
    return (
        state_dir.expanduser().resolve()
        / "approved-uploads"
        / state_segment(job_no)
        / "cleanup.json"
    )


def write_upload_cleanup_journal(
    path: Path,
    worker_id: str,
    job: dict[str, Any],
    upload_video: dict[str, Any],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(
            {
                "schemaVersion": "1.0",
                "workerId": worker_id,
                "job": job,
                "uploadVideo": upload_video,
            },
            ensure_ascii=False,
            indent=2,
        ) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def finish_upload_and_cleanup(
    state_dir: Path,
    backend: str,
    token: str,
    worker_id: str,
    job: dict[str, Any],
    upload_video: dict[str, Any],
) -> dict[str, Any]:
    """Durably confirm the upload before deleting the source video."""
    job_no = str(job.get("jobNo") or "")
    journal = upload_cleanup_journal_path(state_dir, job_no)
    write_upload_cleanup_journal(journal, worker_id, job, upload_video)
    complete_upload(backend, token, worker_id, job_no, upload_video)
    cleanup = cleanup_uploaded_local_file(job, upload_video)
    if cleanup.get("status") != "failed":
        try:
            journal.unlink()
        except FileNotFoundError:
            pass
    return cleanup


def replay_upload_cleanup_journals(
    state_dir: Path,
    backend: str,
    token: str,
    worker_id: str,
) -> list[dict[str, Any]]:
    """Retry callbacks/deletes interrupted after an R2 result was persisted."""
    root = state_dir.expanduser().resolve() / "approved-uploads"
    if not root.is_dir():
        return []
    replayed: list[dict[str, Any]] = []
    for journal in sorted(root.glob("*/cleanup.json")):
        try:
            payload = json.loads(journal.read_text(encoding="utf-8"))
            if payload.get("workerId") != worker_id:
                continue
            job = payload["job"]
            upload_video = payload["uploadVideo"]
            if not isinstance(job, dict) or not isinstance(upload_video, dict):
                raise ValueError("invalid upload cleanup journal")
            cleanup = finish_upload_and_cleanup(
                state_dir, backend, token, worker_id, job, upload_video
            )
            replayed.append({"jobNo": job.get("jobNo"), "localCleanup": cleanup})
        except (BackendError, OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
            print(
                f"Upload cleanup retry warning ({journal.parent.name}): {exc}",
                file=sys.stderr,
                flush=True,
            )
    return replayed


def process_upload_job(
    args: argparse.Namespace,
    backend: str,
    token: str,
    worker_id: str,
    job: dict[str, Any],
) -> dict[str, Any]:
    job_no = state_segment(job.get("jobNo"))
    upload_dir = args.state_dir.expanduser().resolve() / "approved-uploads" / job_no
    upload_dir.mkdir(parents=True, exist_ok=True)
    source_manifest = upload_dir / "source.json"
    result_manifest = upload_dir / "result.json"
    source_manifest.write_text(
        json.dumps(
            {
                "schemaVersion": "1.0",
                "videos": [{
                    "status": "downloaded",
                    "source": job.get("sourceName"),
                    "title": job.get("title"),
                    "originalUrl": job.get("originalUrl"),
                    "canonicalUrl": job.get("canonicalUrl"),
                    "localPath": job.get("localPath"),
                    "fileName": job.get("fileName"),
                    "fileSize": job.get("fileSize"),
                    "sha256": job.get("fileSha256"),
                }],
            },
            ensure_ascii=False,
            indent=2,
        ) + "\n",
        encoding="utf-8",
    )
    command = [
        sys.executable, str(R2_SCRIPT), "--manifest", str(source_manifest),
        "--prefix", job_r2_prefix(job), "--flatten", "--execute",
        "--execution-id", job_no, "--result-json", str(result_manifest),
    ]
    exit_code, output = run_command(command)
    try:
        if not result_manifest.is_file():
            raise PipelineError("R2 skill did not write the approved-video result manifest")
        result = json.loads(result_manifest.read_text(encoding="utf-8"))
        videos = result.get("videos", [])
        if not videos:
            raise PipelineError("R2 skill returned no approved-video result")
        upload_video = videos[0]
    except (OSError, PipelineError, json.JSONDecodeError, TypeError) as exc:
        upload_video = {"status": "failed", "error": str(exc)[:500]}
        result = {"videos": [upload_video]}
    result["localCleanup"] = finish_upload_and_cleanup(
        args.state_dir,
        backend,
        token,
        worker_id,
        job,
        upload_video,
    )
    if result["localCleanup"]["status"] == "failed":
        print(
            f"Local video cleanup warning: {result['localCleanup']['error']}",
            file=sys.stderr,
            flush=True,
        )
    result["exitCode"] = exit_code
    result["output"] = output[-20_000:]
    return result


def drain_upload_jobs(
    args: argparse.Namespace,
    backend: str,
    token: str,
    worker_id: str,
    task_no: str = "",
) -> list[dict[str, Any]]:
    results = replay_upload_cleanup_journals(
        args.state_dir, backend, token, worker_id
    )
    while True:
        job = claim_upload(backend, token, worker_id, task_no)
        if job is None:
            break
        try:
            results.append(process_upload_job(args, backend, token, worker_id, job))
        except BackendError:
            # The durable journal is replayed on the next poll. Stop claiming
            # new work until backend connectivity is healthy.
            raise
        except Exception as exc:
            print(
                f"Approved upload failed ({job.get('jobNo')}): {exc}",
                file=sys.stderr,
                flush=True,
            )
    return results


def local_delete_roots(state_dir: Path) -> list[Path]:
    """Return the same local media roots accepted by the Hermes Gateway."""
    desktop_facebook = Path.home() / "Desktop" / "Facebook"
    configured = [
        state_dir,
        environment_path("HM_INGEST_STATE_DIR", DEFAULT_STATE_DIR),
        environment_path("FACEBOOK_FOLLOWED_OUTPUT", desktop_facebook),
        environment_path("FB_FOLLOWED_DESKTOP", desktop_facebook),
    ]
    roots: list[Path] = []
    for value in configured:
        root = absolute_path_without_resolving_links(value)
        if root not in roots:
            roots.append(root)
    return roots


def resolve_local_delete_path(value: object, state_dir: Path) -> Path:
    """Resolve a backend-queued path without allowing deletion outside media roots."""
    raw_value = str(value or "").strip()
    if not raw_value:
        raise ValueError("local path is missing")
    candidate = absolute_path_without_resolving_links(Path(raw_value))
    if candidate.suffix.lower() not in VIDEO_EXTENSIONS:
        raise ValueError("local path is not a supported video")
    for root in local_delete_roots(state_dir):
        candidate_base = root
        try:
            relative = candidate.relative_to(candidate_base)
        except ValueError:
            candidate_base = root.resolve()
            try:
                relative = candidate.relative_to(candidate_base)
            except ValueError:
                continue
        current = candidate_base
        for segment in relative.parts:
            current /= segment
            if current.is_symlink():
                raise PermissionError(
                    "local video path contains a symbolic link"
                )
        try:
            candidate.resolve(strict=False).relative_to(root.resolve())
        except ValueError as exc:
            raise PermissionError(
                "local video resolves outside configured media roots"
            ) from exc
        return candidate
    raise PermissionError("local video is outside configured media roots")


def process_local_delete_job(
    args: argparse.Namespace,
    backend: str,
    token: str,
    worker_id: str,
    job: dict[str, Any],
) -> dict[str, Any]:
    """Delete one rejected source file locally and report a terminal outcome."""
    job_no = str(job.get("jobNo") or "").strip()
    if state_segment(job_no) != job_no:
        raise PipelineError("backend local-delete jobNo is invalid")

    status = "DELETE_FAILED"
    error_code: str | None = None
    error_message: str | None = None
    file_name: str | None = None
    try:
        job_worker_id = str(job.get("workerId") or "").strip()
        if job_worker_id and job_worker_id != worker_id:
            raise PermissionError("local-delete job belongs to another Worker")
        target = resolve_local_delete_path(job.get("localPath"), args.state_dir)
        file_name = target.name
        try:
            target.unlink()
            status = "DELETED"
        except FileNotFoundError:
            status = "NOT_FOUND"
    except PermissionError as exc:
        error_code = "LOCAL_PATH_FORBIDDEN"
        error_message = str(exc)[:500]
    except (OSError, RuntimeError, ValueError) as exc:
        error_code = "LOCAL_DELETE_FAILED"
        error_message = str(exc)[:500]

    complete_local_delete(
        backend,
        token,
        worker_id,
        job_no,
        status,
        error_code,
        error_message,
    )
    return {
        "jobNo": job_no,
        "videoNo": job.get("videoNo"),
        "taskNo": job.get("taskNo"),
        "status": status,
        "fileName": file_name,
        "errorCode": error_code,
        "errorMessage": error_message,
    }


def drain_local_delete_jobs(
    args: argparse.Namespace,
    backend: str,
    token: str,
    worker_id: str,
) -> list[dict[str, Any]]:
    """Drain a bounded batch so a repeatedly failing job cannot spin forever."""
    results: list[dict[str, Any]] = []
    for _ in range(MAX_LOCAL_DELETE_JOBS_PER_POLL):
        job = claim_local_delete(backend, token, worker_id)
        if job is None:
            break
        try:
            result = process_local_delete_job(
                args, backend, token, worker_id, job
            )
        except BackendError:
            raise
        except Exception as exc:
            print(
                f"Local-delete job failed ({job.get('jobNo')}): {exc}",
                file=sys.stderr,
                flush=True,
            )
            break
        results.append(result)
        if result["status"] == "DELETE_FAILED":
            break
    return results


def queue_work_status(
    uploads: list[dict[str, Any]],
    local_deletes: list[dict[str, Any]],
    local_delete_error: str | None = None,
) -> str:
    if local_delete_error:
        return "queue-work-partial" if uploads or local_deletes else "queue-work-warning"
    if uploads and local_deletes:
        return "queue-work-completed"
    if uploads:
        return "uploads-completed"
    if local_deletes:
        return "local-deletes-completed"
    return "no-work"


def capture_review_status(job: dict[str, Any]) -> str:
    return "AUTO_APPROVED" if bool(job.get("autoReviewEnabled")) else "PENDING_REVIEW"


def safely_drain_local_delete_jobs(
    args: argparse.Namespace,
    backend: str,
    token: str,
    worker_id: str,
) -> tuple[list[dict[str, Any]], str | None]:
    """Keep an unavailable delete API from blocking the established upload queue."""
    try:
        return drain_local_delete_jobs(args, backend, token, worker_id), None
    except BackendError as exc:
        message = str(exc)[:1000]
        print(f"Local-delete queue warning: {message}", file=sys.stderr, flush=True)
        return [], message


def check(args: argparse.Namespace) -> int:
    runtime: dict[str, Any] | None = None
    runtime_error: str | None = None
    try:
        runtime = download_runtime_check()
    except (OSError, subprocess.SubprocessError, PipelineError) as exc:
        runtime_error = str(exc)
    checks = {
        "skill": SKILL_NAME,
        "skillVersion": SKILL_VERSION,
        "backendConfigured": bool(args.backend),
        "workerIdConfigured": bool(args.worker_id),
        "workerTokenConfigured": bool(args.worker_token),
        "mediaBaseUrlConfigured": bool(args.media_base_url),
        "downloadScript": str(DOWNLOAD_SCRIPT),
        "downloadScriptExists": DOWNLOAD_SCRIPT.is_file(),
        "r2Script": str(R2_SCRIPT),
        "r2ScriptExists": R2_SCRIPT.is_file(),
        "stateDir": str(args.state_dir.expanduser().resolve()),
        "r2BucketConfigured": bool(os.getenv("CLOUDFLARE_R2_BUCKET")),
        "r2CredentialsConfigured": bool(
            os.getenv("CLOUDFLARE_R2_ACCESS_KEY_ID")
            and os.getenv("CLOUDFLARE_R2_SECRET_ACCESS_KEY")
        ),
        "boto3Available": importlib.util.find_spec("boto3") is not None,
        "downloadRuntime": runtime,
        "downloadRuntimeReady": bool(runtime and runtime.get("runtimeReady")),
        "downloadRuntimeError": runtime_error,
    }
    checks["ready"] = all(
        checks[key]
        for key in (
            "backendConfigured",
            "workerIdConfigured",
            "workerTokenConfigured",
            "mediaBaseUrlConfigured",
            "downloadScriptExists",
            "downloadRuntimeReady",
            "r2ScriptExists",
            "r2BucketConfigured",
            "r2CredentialsConfigured",
            "boto3Available",
        )
    )
    print(json.dumps(checks, ensure_ascii=False, indent=2))
    return 0 if checks["ready"] else 1


def execute_one(args: argparse.Namespace) -> tuple[int, dict[str, Any]]:
    backend = normalize_backend(args.backend)
    worker_id = args.worker_id.strip()
    token = args.worker_token
    if not worker_id:
        raise PipelineError("HM_WORKER_ID is missing")
    if not token:
        raise PipelineError("HM_WORKER_TOKEN is missing")
    try:
        register_media_endpoint(
            backend, token, worker_id, args.media_base_url
        )
    except BackendError as exc:
        # A rolling backend deployment must not prevent capture or approved
        # upload processing. The next invocation refreshes registration again.
        print(f"Media endpoint registration warning: {exc}", file=sys.stderr)
    if args.upload_only:
        local_deletes, local_delete_error = safely_drain_local_delete_jobs(
            args, backend, token, worker_id
        )
        uploads = drain_upload_jobs(args, backend, token, worker_id, args.task_no)
        result = {
            "schemaVersion": "1.0",
            "skill": SKILL_NAME,
            "status": queue_work_status(
                uploads, local_deletes, local_delete_error
            ),
            "taskNo": args.task_no.strip().upper() or None,
            "uploads": uploads,
            "localDeletes": local_deletes,
        }
        if local_delete_error:
            result["localDeleteError"] = local_delete_error
        return 0, result
    # Refuse to claim durable backend work when an interrupted Skill update or
    # an unsupported Node runtime would make execution fail after assignment.
    download_runtime_check()
    deadline = time.monotonic() + args.wait_for_work_seconds
    while True:
        job = claim(backend, token, worker_id, args.task_no, args.execution_no)
        if job is not None or time.monotonic() >= deadline:
            break
        remaining = deadline - time.monotonic()
        threading.Event().wait(min(args.poll_seconds, max(0, remaining)))
    if job is None:
        local_deletes, local_delete_error = safely_drain_local_delete_jobs(
            args, backend, token, worker_id
        )
        uploads = drain_upload_jobs(args, backend, token, worker_id, args.task_no)
        status = queue_work_status(uploads, local_deletes, local_delete_error)
        if status != "no-work":
            result = {
                "schemaVersion": "1.0",
                "skill": SKILL_NAME,
                "status": status,
                "taskNo": args.task_no.strip().upper() or None,
                "executionNo": args.execution_no.strip().upper() or None,
                "uploads": uploads,
                "localDeletes": local_deletes,
            }
            if local_delete_error:
                result["localDeleteError"] = local_delete_error
            return 0, result
        result = {
            "schemaVersion": "1.0",
            "skill": SKILL_NAME,
            "status": "no-work",
            "taskNo": args.task_no.strip().upper() or None,
            "executionNo": args.execution_no.strip().upper() or None,
            "uploads": uploads,
            "localDeletes": local_deletes,
        }
        if args.execution_no.strip():
            target = f" for execution {args.execution_no.strip().upper()}"
        else:
            target = f" for task {args.task_no.strip().upper()}" if args.task_no.strip() else ""
        if not args.watch:
            print(f"No queued Facebook capture execution{target}.")
        return 0, result

    execution_id = str(job["executionId"])
    raw_parts: list[str] = []
    heartbeat_pump = HeartbeatPump(
        backend,
        token,
        worker_id,
        execution_id,
        args.heartbeat_seconds,
    )
    video_recorder = IncrementalVideoRecorder(
        backend,
        token,
        worker_id,
        execution_id,
        heartbeat_pump.update,
    )
    try:
        heartbeat(backend, token, worker_id, execution_id, 5)
        heartbeat_pump.start()
        execution_dir = (
            args.state_dir.expanduser().resolve()
            / state_segment(execution_id)
        )
        execution_dir.mkdir(parents=True, exist_ok=True)
        download_manifest = execution_dir / "download.json"
        download_result = reusable_download_result(download_manifest)
        if download_result is None:
            video_recorder.start()
            download_command = [
                sys.executable,
                str(DOWNLOAD_SCRIPT),
                "--source",
                str(job["sourceName"]),
                str(job["sourceUrl"]),
                "--mode",
                "daily",
                "--initial-count",
                str(args.count),
                "--max-duration-seconds",
                "1200",
                "--execute",
                "--execution-id",
                execution_id,
                "--result-json",
                str(download_manifest),
            ]
            if args.download_output:
                download_command.extend(["--output", str(args.download_output.expanduser().resolve())])
            download_env = os.environ.copy()
            download_env[VIDEO_RESULT_EVENTS_ENV] = "1"
            download_exit, download_output = run_command(
                download_command,
                env=download_env,
                on_line=video_recorder.handle_line,
            )
            raw_parts.append(download_output)
            if not download_manifest.is_file():
                raise PipelineError("download skill did not write its result manifest")
            download_result = json.loads(download_manifest.read_text(encoding="utf-8"))
        else:
            download_exit = int(download_result.get("exitCode") or 0)
            raw_parts.append(f"Reused durable download manifest: {download_manifest}\n")
        manifest_items = manifest_videos(download_result)
        videos = [
            video for video in manifest_items
            if str(video.get("status") or "").strip().lower()
            not in NON_ACTIONABLE_VIDEO_STATUSES
        ]
        video_recorder.finish(videos)

        heartbeat_pump.update(90)
        heartbeat(backend, token, worker_id, execution_id, 90)
        combined = {
            "schemaVersion": "1.0",
            "skill": SKILL_NAME,
            "skillVersion": SKILL_VERSION,
            "executionId": execution_id,
            "accountName": job.get("accountName") or job.get("sourceName"),
            "region": job.get("region"),
            "category": job.get("category"),
            "download": download_result,
            "review": {
                "status": capture_review_status(job),
                "downloaded": sum(video.get("status") == "downloaded" for video in videos),
                "filteredOver20Minutes": sum(
                    video.get("status") == "filtered-duration"
                    for video in manifest_items
                ),
                "archivedExisting": sum(
                    video.get("status") == "archived-existing"
                    for video in manifest_items
                ),
            },
        }
        successes = sum(video.get("status") == "downloaded" for video in videos)
        failures = sum(video.get("status") != "downloaded" for video in videos)
        if failures == 0 and download_exit == 0:
            terminal_status = "COMPLETED"
        elif successes:
            terminal_status = "PARTIAL"
        else:
            terminal_status = "FAILED"
        combined["status"] = terminal_status
        (execution_dir / "result.json").write_text(
            json.dumps(combined, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        (execution_dir / "worker.log").write_text("".join(raw_parts), encoding="utf-8")
        heartbeat_pump.stop()
        download_error_code = manifest_error_code(download_result)
        complete(
            backend,
            token,
            worker_id,
            execution_id,
            terminal_status,
            combined,
            "".join(raw_parts),
            None
            if terminal_status == "COMPLETED"
            else (download_error_code or "PIPELINE_PARTIAL_OR_FAILED"),
            None if terminal_status == "COMPLETED" else "One or more download items failed",
        )
        try:
            combined["localDeletes"] = drain_local_delete_jobs(
                args, backend, token, worker_id
            )
        except Exception as local_delete_error:
            # Capture completion is already durable. The recurring queue poller
            # can reclaim this delete job after its backend lease expires.
            combined["localDeleteError"] = str(local_delete_error)
        try:
            combined["approvedUploads"] = drain_upload_jobs(
                args, backend, token, worker_id, str(job.get("taskId") or args.task_no)
            )
        except Exception as upload_error:
            # The capture execution is already durably completed. Leave a
            # callback failure for the upload-job lease to retry without
            # incorrectly re-running or downgrading the capture itself.
            combined["approvedUploadError"] = str(upload_error)
        # PARTIAL is a completed orchestration with item-level failures already
        # recorded in HM. Keep Hermes cron healthy and reserve a non-zero exit
        # code for a Worker/pipeline execution that failed as a whole.
        return (0 if terminal_status in {"COMPLETED", "PARTIAL"} else 1), combined
    except Exception as exc:
        video_recorder.stop()
        heartbeat_pump.stop()
        failed = {
            "schemaVersion": "1.0",
            "skill": SKILL_NAME,
            "skillVersion": SKILL_VERSION,
            "executionId": execution_id,
            "status": "FAILED",
            "error": str(exc),
        }
        if isinstance(exc, BackendError) and exc.retryable:
            failed["callbackError"] = str(exc)
            failed["retry"] = "lease"
            return 1, failed
        try:
            complete(
                backend,
                token,
                worker_id,
                execution_id,
                "FAILED",
                failed,
                "".join(raw_parts),
                getattr(exc, "error_code", "PIPELINE_ERROR"),
                str(exc),
            )
        except Exception as callback_error:
            failed["callbackError"] = str(callback_error)
            if isinstance(callback_error, BackendError) and callback_error.retryable:
                failed["retry"] = "lease"
        return 1, failed


def main(argv: list[str] | None = None) -> int:
    load_env_file(infer_hermes_home() / ".env")
    args = build_parser().parse_args(argv)
    if args.check:
        return check(args)
    if args.watch:
        watch_lock = acquire_watch_lock(watch_lock_path(args.state_dir))
        if watch_lock is None:
            print("Customer-side HM Worker is already running.")
            return 0
        print(
            f"Watching {args.backend or '<missing backend>'} as "
            f"{args.worker_id or '<missing worker id>'}; poll interval {args.poll_seconds}s.",
            flush=True,
        )
        try:
            while True:
                try:
                    exit_code, result = execute_one(args)
                except (OSError, ValueError, PipelineError, json.JSONDecodeError, KeyError) as exc:
                    print(f"Worker poll failed: {exc}", file=sys.stderr, flush=True)
                    threading.Event().wait(args.poll_seconds)
                    continue
                if args.json:
                    print(json.dumps(result, ensure_ascii=False, indent=2))
                if result.get("status") in {"no-work", "queue-work-warning"}:
                    threading.Event().wait(args.poll_seconds)
                elif exit_code:
                    print("Job failed; result was recorded. Continuing to poll.", file=sys.stderr)
        except KeyboardInterrupt:
            print("Worker stopped.")
            return 0
        finally:
            watch_lock.close()
    if not args.execute:
        raise PipelineError(
            "Preview is not supported for backend claiming; use --check, --execute, or --watch"
        )
    exit_code, result = execute_one(args)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    return exit_code


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, PipelineError, json.JSONDecodeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(2)
