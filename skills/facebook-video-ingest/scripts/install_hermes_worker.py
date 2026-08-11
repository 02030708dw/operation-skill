#!/usr/bin/env python3
"""Prepare Hermes for HM-managed, task-specific video ingest Cron jobs."""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path


LEGACY_JOB_NAME = "HM 视频抓取 Worker"
DEFAULT_SCHEDULE = "every 1m"
RUNNER_NAME = "hm_facebook_video_ingest_worker.py"
ANSI_PATTERN = re.compile(r"\x1b\[[0-9;]*m")


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="Install or inspect the HM Facebook ingest Hermes Worker task"
    )
    result.add_argument("--schedule", default=DEFAULT_SCHEDULE)
    result.add_argument("--check", action="store_true")
    result.add_argument("--run-now", action="store_true")
    return result


def hermes_home() -> Path:
    return Path(os.getenv("HERMES_HOME", str(Path.home() / ".hermes"))).expanduser()


def hermes_command() -> str:
    found = shutil.which("hermes")
    if found:
        return found
    candidate = Path.home() / ".local" / "bin" / "hermes"
    if candidate.is_file():
        return str(candidate)
    raise RuntimeError("Hermes CLI is not installed or is not on PATH")


def call(command: list[str], *, allow_failure: bool = False) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(command, text=True, capture_output=True, check=False)
    if completed.stdout:
        print(completed.stdout, end="")
    if completed.stderr:
        print(completed.stderr, end="", file=sys.stderr)
    combined = ANSI_PATTERN.sub("", f"{completed.stdout}\n{completed.stderr}")
    reported_failure = any(
        line.strip().startswith("Failed to ")
        for line in combined.splitlines()
    )
    if (completed.returncode or reported_failure) and not allow_failure:
        raise RuntimeError(
            f"command failed ({completed.returncode}): {' '.join(command[:3])}"
        )
    return completed


def has_legacy_job(list_output: str) -> bool:
    normalized = ANSI_PATTERN.sub("", list_output)
    return any(
        line.strip() == f"Name:      {LEGACY_JOB_NAME}"
        for line in normalized.splitlines()
    )


def install_runner(home: Path) -> Path:
    source = Path(__file__).resolve().with_name("hermes_cron_runner.py")
    if not source.is_file():
        raise RuntimeError(f"cron runner is missing: {source}")
    target_dir = home / "scripts"
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / RUNNER_NAME
    source_bytes = source.read_bytes()
    if not target.is_file() or target.read_bytes() != source_bytes:
        target.write_bytes(source_bytes)
    target.chmod(0o700)
    return target.resolve()


def hermes_python(home: Path) -> Path:
    candidates = [
        home / "hermes-agent" / "venv" / "bin" / "python",
        home / "hermes-agent" / "venv" / "Scripts" / "python.exe",
    ]
    for candidate in candidates:
        if candidate.is_file():
            # Keep the virtual-environment entry path. Resolving its symlink
            # points at Hermes' uv-managed base interpreter and triggers PEP
            # 668 instead of installing into the isolated Hermes environment.
            return candidate
    raise RuntimeError("Hermes Python environment was not found")


def ensure_dependencies(home: Path) -> None:
    requirements = (
        Path(__file__).resolve().parents[2]
        / "cloudflare-r2-video-upload"
        / "scripts"
        / "requirements.txt"
    )
    if not requirements.is_file():
        raise RuntimeError(f"R2 requirements are missing: {requirements}")
    python = hermes_python(home)
    probe = subprocess.run(
        [str(python), "-c", "import boto3"],
        text=True,
        capture_output=True,
        check=False,
    )
    if probe.returncode:
        call(
            [
                str(python),
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "-r",
                str(requirements),
            ]
        )


def gateway_is_running(output: str) -> bool:
    normalized = ANSI_PATTERN.sub("", output).lower()
    if "gateway is not running" in normalized:
        return False
    return any(
        marker in normalized
        for marker in (
            "gateway is running",
            "gateway is supervised",
            "service started",
        )
    )


def ensure_gateway(hermes: str) -> None:
    status = call([hermes, "gateway", "status"], allow_failure=True)
    if gateway_is_running(f"{status.stdout}\n{status.stderr}"):
        return
    call(
        [
            hermes,
            "gateway",
            "install",
            "--start-now",
            "--start-on-login",
        ]
    )
    status = call([hermes, "gateway", "status"], allow_failure=True)
    if not gateway_is_running(f"{status.stdout}\n{status.stderr}"):
        call([hermes, "gateway", "start"])
        status = call([hermes, "gateway", "status"], allow_failure=True)
    if not gateway_is_running(f"{status.stdout}\n{status.stderr}"):
        raise RuntimeError("Hermes Gateway did not start")


def remove_legacy_job(hermes: str) -> None:
    listed = call([hermes, "cron", "list", "--all"])
    if has_legacy_job(listed.stdout):
        call([hermes, "cron", "remove", LEGACY_JOB_NAME])


def trigger_worker(home: Path, runner: Path) -> None:
    subprocess.Popen(
        [str(hermes_python(home)), str(runner)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    hermes = hermes_command()
    home = hermes_home()
    if not args.check:
        runner = install_runner(home)
        ensure_dependencies(home)
        ensure_gateway(hermes)
        remove_legacy_job(hermes)
        if args.run_now:
            trigger_worker(home, runner)
    call([hermes, "gateway", "status"], allow_failure=True)
    call([hermes, "cron", "list", "--all"])
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"Worker installation failed: {exc}", file=sys.stderr)
        raise SystemExit(2)
