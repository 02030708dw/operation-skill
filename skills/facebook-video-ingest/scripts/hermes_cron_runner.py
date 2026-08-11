#!/usr/bin/env python3
"""Hermes cron entry point for the backend-managed Facebook ingest Worker."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path
from typing import IO


TASK_RUNNER_PATTERN = re.compile(
    r"^hm_capture_(C-[A-Za-z0-9-]+)_(?:\d{4}|immediate)$",
    re.IGNORECASE,
)
EXECUTION_RUNNER_PATTERN = re.compile(
    r"^hm_capture_(C-[A-Za-z0-9-]+)_(E-[A-Za-z0-9-]+)$",
    re.IGNORECASE,
)


def hermes_home() -> Path:
    return Path(os.getenv("HERMES_HOME", str(Path.home() / ".hermes"))).expanduser()


def load_env_file(path: Path) -> None:
    """Load missing values from Hermes' private .env without printing secrets."""
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


def find_worker_script(home: Path) -> Path:
    worker = (
        home
        / "skills"
        / "operation-skill"
        / "facebook-video-ingest"
        / "scripts"
        / "facebook_video_ingest.py"
    )
    if worker.is_file():
        return worker.resolve()
    raise FileNotFoundError(
        "operation-skill facebook-video-ingest Worker not found: " f"{worker}"
    )


def acquire_worker_lock(path: Path) -> IO[str] | None:
    """Hold one non-blocking machine-local Worker lock for this process."""
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


def task_no_from_runner(path: Path) -> str | None:
    match = EXECUTION_RUNNER_PATTERN.fullmatch(path.stem)
    if match:
        return match.group(1).upper()
    match = TASK_RUNNER_PATTERN.fullmatch(path.stem)
    return match.group(1).upper() if match else None


def execution_no_from_runner(path: Path) -> str | None:
    match = EXECUTION_RUNNER_PATTERN.fullmatch(path.stem)
    return match.group(2).upper() if match else None


def worker_command(worker: Path, runner: Path) -> list[str]:
    command = [sys.executable, str(worker), "--execute"]
    task_no = task_no_from_runner(runner)
    if task_no:
        command.extend(
            ["--task-no", task_no, "--wait-for-work-seconds", "30"]
        )
    execution_no = execution_no_from_runner(runner)
    if execution_no:
        command.extend(["--execution-no", execution_no])
    command.append("--json")
    return command


def worker_lock_path(home: Path, runner: Path) -> Path:
    execution_no = execution_no_from_runner(runner)
    if execution_no:
        return home / "facebook-video-ingest" / f"worker-{execution_no}.lock"
    task_no = task_no_from_runner(runner)
    lock_name = f"worker-{task_no}.lock" if task_no else "worker.lock"
    return home / "facebook-video-ingest" / lock_name


def main() -> int:
    home = hermes_home()
    load_env_file(home / ".env")
    runner = Path(__file__)
    worker_lock = acquire_worker_lock(worker_lock_path(home, runner))
    if worker_lock is None:
        return 0
    worker = find_worker_script(home)
    try:
        completed = subprocess.run(
            worker_command(worker, runner),
            text=True,
            check=False,
        )
        return completed.returncode
    finally:
        worker_lock.close()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError) as exc:
        print(f"HM Worker cron failed: {exc}", file=sys.stderr)
        raise SystemExit(2)
