#!/usr/bin/env python3
"""Claim one HM capture execution, download Facebook videos, upload them to R2, and report results."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import IO, Any
from urllib import error, request


SKILL_NAME = "facebook-video-ingest"
WORKER_USER_AGENT = "HM-Hermes-Worker/1.0"
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
    pass


class BackendError(PipelineError):
    pass


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
        default=Path(os.getenv("HM_INGEST_STATE_DIR", str(DEFAULT_STATE_DIR))),
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
        help="skip capture claiming and immediately drain approved upload jobs",
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
    outgoing = request.Request(
        f"{backend}{path}", data=body, headers=headers, method=method
    )
    try:
        with request.urlopen(outgoing, timeout=30) as response:
            parsed = json.loads(response.read().decode("utf-8"))
    except error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:1000]
        raise BackendError(f"backend returned HTTP {exc.code}: {detail}") from exc
    except (error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        raise BackendError(f"backend request failed: {exc}") from exc
    if not isinstance(parsed, dict) or parsed.get("code") != 200:
        raise BackendError(f"backend rejected request: {parsed}")
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
    )


def heartbeat(backend: str, token: str, worker_id: str, execution_id: str, progress: int) -> None:
    api_call(
        backend,
        token,
        "POST",
        f"/api/internal/capture/executions/{execution_id}/heartbeat",
        {"workerId": worker_id, "progress": progress},
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
        "title": video.get("title") or video.get("fileName") or video.get("platformVideoId"),
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
        "errorMessage": video.get("error"),
        "metadataJson": json.dumps(video, ensure_ascii=False),
    }
    api_call(
        backend,
        token,
        "POST",
        f"/api/internal/capture/executions/{execution_id}/videos",
        payload,
        worker_id=worker_id,
    )


def status_error_code(download_status: str, upload_status: str) -> str | None:
    if download_status == "DOWNLOAD_FAILED":
        return "DOWNLOAD_FAILED"
    if upload_status in {"R2_CONFLICT", "UPLOAD_FAILED"}:
        return upload_status
    return None


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
            "rawOutput": raw_output[-1_000_000:],
            "errorCode": error_code,
            "errorMessage": error_message,
        },
    )


def run_command(command: list[str]) -> tuple[int, str]:
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    lines: list[str] = []
    assert process.stdout is not None
    for line in process.stdout:
        print(line, end="", flush=True)
        lines.append(line)
    return process.wait(), "".join(lines)


def manifest_videos(payload: dict[str, Any]) -> list[dict[str, Any]]:
    videos: list[dict[str, Any]] = []
    for source in payload.get("sources", []):
        for video in source.get("videos", []):
            item = dict(video)
            item.setdefault("source", source.get("name"))
            videos.append(item)
    return videos


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
    if not result_manifest.is_file():
        raise PipelineError("R2 skill did not write the approved-video result manifest")
    result = json.loads(result_manifest.read_text(encoding="utf-8"))
    videos = result.get("videos", [])
    if not videos:
        raise PipelineError("R2 skill returned no approved-video result")
    complete_upload(backend, token, worker_id, str(job["jobNo"]), videos[0])
    result["localCleanup"] = cleanup_uploaded_local_file(job, videos[0])
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
    results: list[dict[str, Any]] = []
    while True:
        job = claim_upload(backend, token, worker_id, task_no)
        if job is None:
            break
        results.append(process_upload_job(args, backend, token, worker_id, job))
    return results


def check(args: argparse.Namespace) -> int:
    checks = {
        "skill": SKILL_NAME,
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
    }
    checks["ready"] = all(
        checks[key]
        for key in (
            "backendConfigured",
            "workerIdConfigured",
            "workerTokenConfigured",
            "mediaBaseUrlConfigured",
            "downloadScriptExists",
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
        uploads = drain_upload_jobs(args, backend, token, worker_id, args.task_no)
        return 0, {
            "schemaVersion": "1.0",
            "skill": SKILL_NAME,
            "status": "uploads-completed" if uploads else "no-work",
            "taskNo": args.task_no.strip().upper() or None,
            "uploads": uploads,
        }
    deadline = time.monotonic() + args.wait_for_work_seconds
    while True:
        job = claim(backend, token, worker_id, args.task_no, args.execution_no)
        if job is not None or time.monotonic() >= deadline:
            break
        remaining = deadline - time.monotonic()
        threading.Event().wait(min(args.poll_seconds, max(0, remaining)))
    if job is None:
        uploads = drain_upload_jobs(args, backend, token, worker_id, args.task_no)
        if uploads:
            return 0, {
                "schemaVersion": "1.0",
                "skill": SKILL_NAME,
                "status": "uploads-completed",
                "uploads": uploads,
            }
        result = {
            "schemaVersion": "1.0",
            "skill": SKILL_NAME,
            "status": "no-work",
            "taskNo": args.task_no.strip().upper() or None,
            "executionNo": args.execution_no.strip().upper() or None,
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
            download_exit, download_output = run_command(download_command)
            raw_parts.append(download_output)
            if not download_manifest.is_file():
                raise PipelineError("download skill did not write its result manifest")
            download_result = json.loads(download_manifest.read_text(encoding="utf-8"))
        else:
            download_exit = int(download_result.get("exitCode") or 0)
            raw_parts.append(f"Reused durable download manifest: {download_manifest}\n")
        videos = [
            video for video in manifest_videos(download_result)
            if video.get("status") != "filtered-duration"
        ]
        for video in videos:
            record_video(
                backend,
                token,
                worker_id,
                execution_id,
                video,
                download_status=(
                    "DOWNLOADED" if video.get("status") == "downloaded" else "DOWNLOAD_FAILED"
                ),
                upload_status="PENDING",
            )

        heartbeat_pump.update(90)
        heartbeat(backend, token, worker_id, execution_id, 90)
        combined = {
            "schemaVersion": "1.0",
            "skill": SKILL_NAME,
            "executionId": execution_id,
            "accountName": job.get("accountName") or job.get("sourceName"),
            "region": job.get("region"),
            "category": job.get("category"),
            "download": download_result,
            "review": {
                "status": "PENDING_REVIEW",
                "downloaded": sum(video.get("status") == "downloaded" for video in videos),
                "filteredOver20Minutes": sum(
                    video.get("status") == "filtered-duration"
                    for video in manifest_videos(download_result)
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
        complete(
            backend,
            token,
            worker_id,
            execution_id,
            terminal_status,
            combined,
            "".join(raw_parts),
            None if terminal_status == "COMPLETED" else "PIPELINE_PARTIAL_OR_FAILED",
            None if terminal_status == "COMPLETED" else "One or more download items failed",
        )
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
        heartbeat_pump.stop()
        failed = {
            "schemaVersion": "1.0",
            "skill": SKILL_NAME,
            "executionId": execution_id,
            "status": "FAILED",
            "error": str(exc),
        }
        if isinstance(exc, BackendError):
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
                "PIPELINE_ERROR",
                str(exc)[:500],
            )
        except Exception as callback_error:
            failed["callbackError"] = str(callback_error)
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
                if result.get("status") == "no-work":
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
