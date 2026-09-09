#!/usr/bin/env python3
"""Trusted Hermes server runner; slots, leases and subprocesses survive browser exit."""
from __future__ import annotations
import base64
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request


def validate_spec(spec: dict) -> dict:
    if not isinstance(spec, dict):
        raise ValueError("Server runner specification must be an object")
    result = {key: spec.get(key) for key in
              ("dispatchId", "attempt", "kind", "slot", "taskNo", "executionNo")}
    tenant = spec.get("tenant")
    if tenant is not None:
        if tenant not in {"ph", "th", "vn", "id"}:
            raise ValueError("Invalid execution tenant")
        result["tenant"] = tenant
    if os.getenv("HM_TENANT_CONFIG"):
        tenant_config(result)  # Reject missing/unconfigured tenants before creating a script.
    for key in ("dispatchId", "attempt", "slot"):
        if type(result[key]) is not int or result[key] < 1:
            raise ValueError(f"Invalid {key}")
    if result["kind"] not in {"CAPTURE", "UPLOAD", "DELETE", "GENERATION"}:
        raise ValueError("Invalid server work kind")
    limit = int(os.getenv("HM_CAPTURE_SLOTS", "8")) if result["kind"] == "CAPTURE" else 1
    if result["slot"] > limit:
        raise ValueError("Server slot exceeds configured capacity")
    if result["kind"] == "CAPTURE":
        for field, prefix in (("taskNo", "C"), ("executionNo", "E")):
            if not re.fullmatch(prefix + r"-[A-Za-z0-9-]+", str(result[field] or "")):
                raise ValueError(f"Invalid {field}")
    return result


def state_root() -> Path:
    return Path(os.getenv("HM_SERVER_STATE_DIR", str(Path(os.environ.get("HERMES_HOME", Path.home()/".hermes"))/"server")))


def tenant_config(spec: dict) -> dict:
    config = os.getenv("HM_TENANT_CONFIG")
    if not config:
        if spec.get("tenant"):
            raise ValueError("Regional worker registry is not configured")
        return {}
    tenants = json.loads(Path(config).read_text())
    tenant = spec.get("tenant")
    if tenant not in tenants:
        raise ValueError("Execution tenant is not configured")
    return tenants[tenant]


def tenant_root(spec: dict) -> Path:
    return state_root() / "tenants" / spec["tenant"] if spec.get("tenant") else state_root()


def worker_environment(spec: dict) -> dict:
    env = os.environ.copy()
    root = tenant_root(spec)
    config = tenant_config(spec)
    if config:
        media = Path(config["mediaRoot"])
        env.update(HM_BACKEND_URL=config["backendUrl"], HM_WORKER_TOKEN=config["workerToken"],
                   HM_WORKER_ID=config["workerId"], HM_CAPTURE_MEDIA_TOKEN=config["mediaToken"],
                   HM_R2_KEY_PREFIX=config["r2Prefix"], HM_SERVER_STATE_DIR=str(root),
                   HM_INGEST_STATE_DIR=str(media / "executions"),
                   FACEBOOK_FOLLOWED_OUTPUT=str(media / "downloads"),
                   FB_FOLLOWED_DESKTOP=str(media / "downloads"))
    env["HM_SERVER_COMPONENT"] = spec["kind"]
    # This isolates both the downloader's global lock and its persistent browser profile.
    env["FACEBOOK_FOLLOWED_STATE_DIR"] = str(root / "slots" / str(spec["slot"]))
    env["TMPDIR"] = str(root / "slots" / str(spec["slot"]) / "tmp")
    env["FACEBOOK_FOLLOWED_REPORTS"] = str(root / "reports" / str(spec["dispatchId"]))
    return env


def api(path: str, body: dict) -> object:
    request = urllib.request.Request(
        os.environ["HM_BACKEND_URL"].rstrip("/") + path,
        data=json.dumps(body).encode(), method="POST",
        headers={"Content-Type": "application/json", "X-HM-Worker-Token": os.environ["HM_WORKER_TOKEN"]},
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        payload = json.load(response)
        return payload.get("data")


def status(spec: dict, state: str, error: str | None = None) -> None:
    api(f'/api/internal/server/dispatches/{spec["dispatchId"]}/status',
        {"attempt": spec["attempt"], "state": state, "error": error})


def lock_file(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        return None
    return handle


def launch(spec: dict) -> None:
    spec = validate_spec(spec)
    root = tenant_root(spec)
    logs = root / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    encoded = base64.urlsafe_b64encode(json.dumps(spec).encode()).decode()
    with (logs / f'{spec["dispatchId"]}-{spec["attempt"]}.log').open("ab") as log:
        subprocess.Popen([sys.executable, str(Path(__file__).resolve()), encoded],
                         stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
    print(json.dumps({"started": True, "dispatchId": spec["dispatchId"]}))


def command(spec: dict) -> list[str]:
    scripts = Path(__file__).resolve().parent
    if spec["kind"] == "GENERATION":
        return [sys.executable, str(scripts / "hm_lottery_generation_worker.py")]
    if spec["kind"] == "DELETE":
        return [sys.executable, str(scripts / "hm_server_worker.py"), "delete"]
    args = [sys.executable, str(scripts / "facebook_video_ingest.py"), "--execute", "--json"]
    if spec["kind"] == "UPLOAD":
        return args + ["--upload-only"]
    return args + ["--task-no", spec["taskNo"], "--execution-no", spec["executionNo"]]


def run(spec: dict) -> int:
    spec = validate_spec(spec)
    root = state_root()
    execution_root = tenant_root(spec)
    slots = [spec["slot"]]
    if spec.get("tenant") and spec["kind"] == "CAPTURE":
        slots += [slot for slot in range(1, int(os.getenv("HM_CAPTURE_SLOTS", "8")) + 1) if slot != spec["slot"]]
    slot_lock = None
    for slot in slots:
        slot_lock = lock_file(root / "locks" / f'{spec["kind"]}-{slot}.lock')
        if slot_lock:
            spec = dict(spec, slot=slot)
            break
    environment = worker_environment(spec)
    Path(environment["TMPDIR"]).mkdir(parents=True, exist_ok=True)
    # The runner's status callbacks and the child use the same trusted registry.
    if spec.get("tenant"):
        os.environ.update(environment)
    execution_lock = None
    child = None
    try:
        if slot_lock is None:
            status(spec, "RETRY")  # Shared-pool contention is normal queueing, not an outage.
            return 0
        if spec["executionNo"]:
            execution_lock = lock_file(execution_root / "locks" / f'{spec["executionNo"]}.lock')
            if execution_lock is None:
                status(spec, "RETRY")
                return 0
        if spec["kind"] in {"CAPTURE", "GENERATION"}:
            media = Path(os.environ.get("HM_INGEST_STATE_DIR", root))
            media.mkdir(parents=True, exist_ok=True)
            minimum = int(os.getenv("HM_MIN_FREE_DISK_BYTES", str(20*1024**3)))
            if shutil.disk_usage(media).free < minimum:
                status(spec, "RETRY", "服务器剩余存储不足，已暂停新下载和生成")
                return 0
        status(spec, "RUNNING")  # Reject stale attempts before claiming business work.
        child = subprocess.Popen(command(spec), env=environment, start_new_session=True)
        last_ack = time.monotonic()
        while child.poll() is None:
            time.sleep(2)
            if time.monotonic() - last_ack < 25:
                continue
            try:
                status(spec, "RUNNING")
                last_ack = time.monotonic()
            except urllib.error.HTTPError as error:
                if error.code in {401, 403, 409}:
                    raise RuntimeError("执行器租约失效，已终止子进程") from error
                if time.monotonic()-last_ack > 90:
                    raise RuntimeError("无法续租，已终止子进程") from error
            except OSError:
                if time.monotonic()-last_ack > 90:
                    raise RuntimeError("后台连接中断，已终止子进程")
        result = child.returncode
        status(spec, "DONE" if result == 0 else "RETRY",
               None if result == 0 else f"执行脚本退出码 {result}，详情见该执行日志")
        return result
    finally:
        if child is not None and child.poll() is None:
            os.killpg(child.pid, signal.SIGTERM)
            try: child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(child.pid, signal.SIGKILL)
                child.wait()
        if execution_lock: execution_lock.close()
        if slot_lock: slot_lock.close()


def delete_jobs() -> None:
    import facebook_video_ingest as ingest
    args = ingest.build_parser().parse_args(["--execute"])
    ingest.drain_local_delete_jobs(args, ingest.normalize_backend(args.backend), args.worker_token, args.worker_id)


if __name__ == "__main__":
    if len(sys.argv) == 2 and sys.argv[1] == "delete":
        delete_jobs()
    else:
        raise SystemExit(run(json.loads(base64.urlsafe_b64decode(sys.argv[1]))))
