#!/usr/bin/env python3
"""Configure customer-side Hermes for browser-triggered, on-demand HM Workers."""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from urllib import error, request


LEGACY_JOB_NAME = "HM 视频抓取 Worker"
WORKER_JOB_NAME = "HM 后台任务接收 Worker"
RUNNER_NAME = "hm_facebook_video_ingest_worker.py"
GATEWAY_EXTENSION_SOURCE = "hm_capture_gateway_extension.py"
GATEWAY_EXTENSION_TARGET = "hm_capture_extension.py"
DEFAULT_API_HOST = "127.0.0.1"
DEFAULT_API_PORT = 8642
DEFAULT_ADMIN_ORIGINS = (
    "https://hermes.mvkbmb.online",
    "https://live-gateway.mvkbmb.online",
    "http://127.0.0.1:8848",
    "http://localhost:8848",
)
PAIRING_PREFIX = "HMHERMES1."
ANSI_PATTERN = re.compile(r"\x1b\[[0-9;]*m")
SUBPROCESS_ENCODING = "utf-8"
SUBPROCESS_ERRORS = "replace"


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="Install or inspect the HM on-demand Hermes Worker bridge"
    )
    result.add_argument("--check", action="store_true")
    result.add_argument("--show-pairing-code", action="store_true")
    result.add_argument("--no-pairing-code", action="store_true")
    result.add_argument("--api-port", type=int, default=DEFAULT_API_PORT)
    result.add_argument(
        "--admin-origin",
        action="append",
        dest="admin_origins",
        help="Allowed HM admin browser origin; repeat for multiple origins",
    )
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


def call(
    command: list[str],
    *,
    allow_failure: bool = False,
    interactive: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Run a Hermes command without relying on the Windows ANSI code page.

    Hermes emits UTF-8 even when native Windows reports GBK/CP936 as its
    preferred encoding.  Decoding through the locale can therefore crash a
    ``subprocess.run`` reader thread.  Gateway installation is interactive on
    Windows when UAC approval is needed, so leave its console attached.
    """
    completed = subprocess.run(
        command,
        text=True,
        encoding=SUBPROCESS_ENCODING,
        errors=SUBPROCESS_ERRORS,
        capture_output=not interactive,
        check=False,
    )
    if completed.stdout:
        print(completed.stdout, end="")
    if completed.stderr:
        print(completed.stderr, end="", file=sys.stderr)
    combined = ANSI_PATTERN.sub("", f"{completed.stdout}\n{completed.stderr}")
    reported_failure = any(
        line.strip().startswith("Failed to ") for line in combined.splitlines()
    )
    if (completed.returncode or reported_failure) and not allow_failure:
        raise RuntimeError(
            f"command failed ({completed.returncode}): {' '.join(command[:3])}"
        )
    return completed


def has_job(list_output: str, name: str) -> bool:
    normalized = ANSI_PATTERN.sub("", list_output)
    return any(
        line.strip() == f"Name:      {name}" for line in normalized.splitlines()
    )


def has_legacy_job(list_output: str) -> bool:
    return has_job(list_output, LEGACY_JOB_NAME)


def obsolete_job_names(list_output: str) -> list[str]:
    """Return only jobs from the old continuous-receiver architecture."""
    normalized = ANSI_PATTERN.sub("", list_output)
    names: list[str] = []
    for line in normalized.splitlines():
        trimmed = line.strip()
        if not trimmed.startswith("Name:"):
            continue
        name = trimmed[len("Name:") :].strip()
        if name in {LEGACY_JOB_NAME, WORKER_JOB_NAME} or name.startswith(
            "HM 立即抓取 C-"
        ):
            names.append(name)
    return names


def install_runner(home: Path) -> Path:
    """Keep the deterministic runner installed for migrated no-agent jobs."""
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


def _replace_once(source: str, old: str, new: str, label: str) -> str:
    count = source.count(old)
    if count != 1:
        raise RuntimeError(
            f"Hermes Gateway API patch point changed ({label}, found {count})"
        )
    return source.replace(old, new, 1)


def patch_gateway_api_source(source: str) -> str:
    """Add standard no-agent fields plus the restricted HM runner hook."""
    marker = "prepare_capture_job_body(await request.json())"
    if marker in source:
        return source

    create_start = source.find("    async def _handle_create_job(")
    create_end = source.find("    async def _handle_get_job(", create_start)
    if create_start < 0 or create_end < 0:
        raise RuntimeError("Hermes Gateway create-job handler was not found")
    create_block = source[create_start:create_end]
    create_block = _replace_once(
        create_block,
        "            body = await request.json()\n"
        "            name = (body.get(\"name\") or \"\").strip()\n",
        "            from gateway.platforms.hm_capture_extension import (\n"
        "                prepare_capture_job_body,\n"
        "            )\n\n"
        "            body = prepare_capture_job_body(await request.json())\n"
        "            name = (body.get(\"name\") or \"\").strip()\n",
        "prepare create body",
    )
    create_block = _replace_once(
        create_block,
        "            skills = body.get(\"skills\")\n"
        "            repeat = body.get(\"repeat\")\n",
        "            skills = body.get(\"skills\")\n"
        "            repeat = body.get(\"repeat\")\n"
        "            script = (body.get(\"script\") or \"\").strip()\n"
        "            no_agent = bool(body.get(\"no_agent\"))\n",
        "read script fields",
    )
    create_block = _replace_once(
        create_block,
        "            if skills:\n"
        "                kwargs[\"skills\"] = skills\n"
        "            if repeat is not None:\n",
        "            if skills:\n"
        "                kwargs[\"skills\"] = skills\n"
        "            if script:\n"
        "                kwargs[\"script\"] = script\n"
        "            if no_agent:\n"
        "                if not script:\n"
        "                    return web.json_response(\n"
        "                        {\"error\": \"no_agent requires a script\"}, status=400,\n"
        "                    )\n"
        "                kwargs[\"no_agent\"] = True\n"
        "            if repeat is not None:\n",
        "forward script fields",
    )
    source = source[:create_start] + create_block + source[create_end:]

    delete_start = source.find("    async def _handle_delete_job(")
    delete_end = source.find("    async def _handle_pause_job(", delete_start)
    if delete_start < 0 or delete_end < 0:
        raise RuntimeError("Hermes Gateway delete-job handler was not found")
    delete_block = source[delete_start:delete_end]
    delete_block = _replace_once(
        delete_block,
        "        try:\n"
        "            success = _cron_remove(job_id)\n"
        "            if not success:\n"
        "                return web.json_response({\"error\": \"Job not found\"}, status=404)\n"
        "            _notify_cron_provider_jobs_changed()\n"
        "            return web.json_response({\"ok\": True})\n",
        "        try:\n"
        "            job = _cron_get(job_id)\n"
        "            success = _cron_remove(job_id)\n"
        "            if not success:\n"
        "                return web.json_response({\"error\": \"Job not found\"}, status=404)\n"
        "            try:\n"
        "                from gateway.platforms.hm_capture_extension import (\n"
        "                    cleanup_capture_job_script,\n"
        "                )\n\n"
        "                cleanup_capture_job_script(job)\n"
        "            except Exception:\n"
        "                logger.debug(\n"
        "                    \"HM capture runner cleanup failed\", exc_info=True\n"
        "                )\n"
        "            _notify_cron_provider_jobs_changed()\n"
        "            return web.json_response({\"ok\": True})\n",
        "cleanup deleted runner",
    )
    source = source[:delete_start] + delete_block + source[delete_end:]
    return source


def install_gateway_api_extension(home: Path) -> Path:
    """Install and idempotently patch Hermes' authenticated loopback API."""
    source_extension = Path(__file__).resolve().with_name(
        GATEWAY_EXTENSION_SOURCE
    )
    if not source_extension.is_file():
        raise RuntimeError(
            f"Gateway extension is missing: {source_extension}"
        )
    platform_dir = home / "hermes-agent" / "gateway" / "platforms"
    api_server = platform_dir / "api_server.py"
    if not api_server.is_file():
        raise RuntimeError(f"Hermes Gateway API source is missing: {api_server}")

    platform_dir.mkdir(parents=True, exist_ok=True)
    extension_target = platform_dir / GATEWAY_EXTENSION_TARGET
    extension_bytes = source_extension.read_bytes()
    if (
        not extension_target.is_file()
        or extension_target.read_bytes() != extension_bytes
    ):
        extension_target.write_bytes(extension_bytes)

    original = api_server.read_text(encoding="utf-8")
    patched = patch_gateway_api_source(original)
    compile(patched, str(api_server), "exec")
    if patched != original:
        backup = api_server.with_suffix(".py.hm-before-capture")
        if not backup.exists():
            shutil.copy2(api_server, backup)
        temporary = api_server.with_suffix(".py.hm-capture.tmp")
        try:
            temporary.write_text(patched, encoding="utf-8")
            os.replace(temporary, api_server)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
    return extension_target.resolve()


def hermes_python(home: Path) -> Path:
    candidates = [
        home / "hermes-agent" / "venv" / "bin" / "python",
        home / "hermes-agent" / "venv" / "Scripts" / "python.exe",
    ]
    for candidate in candidates:
        if candidate.is_file():
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
        encoding=SUBPROCESS_ENCODING,
        errors=SUBPROCESS_ERRORS,
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
            "gateway process running",
            "gateway already running",
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
        ],
        interactive=True,
    )
    status = call([hermes, "gateway", "status"], allow_failure=True)
    if not gateway_is_running(f"{status.stdout}\n{status.stderr}"):
        call([hermes, "gateway", "start"])
        status = call([hermes, "gateway", "status"], allow_failure=True)
    if not gateway_is_running(f"{status.stdout}\n{status.stderr}"):
        raise RuntimeError("Hermes Gateway did not start")


def parse_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, separator, value = line.partition("=")
        key = key.strip()
        if not separator or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[key] = value
    return values


def write_env_values(path: Path, updates: dict[str, str]) -> None:
    """Update selected dotenv keys while preserving unrelated customer settings."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
    remaining = dict(updates)
    next_lines: list[str] = []
    for line in lines:
        match = re.match(r"^(\s*(?:export\s+)?)([A-Za-z_][A-Za-z0-9_]*)(\s*=).*$", line)
        if not match or match.group(2) not in remaining:
            next_lines.append(line)
            continue
        key = match.group(2)
        next_lines.append(f"{key}={remaining.pop(key)}")
    if next_lines and next_lines[-1].strip():
        next_lines.append("")
    next_lines.extend(f"{key}={value}" for key, value in remaining.items())
    path.write_text("\n".join(next_lines).rstrip() + "\n", encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass


def configure_api_server(
    home: Path, *, port: int, admin_origins: list[str]
) -> tuple[str, str]:
    if port < 1 or port > 65535:
        raise ValueError("API port must be between 1 and 65535")
    env_path = home / ".env"
    current = parse_env_file(env_path)
    api_key = current.get("API_SERVER_KEY", "").strip()
    if len(api_key) < 16:
        api_key = secrets.token_urlsafe(32)
    existing_origins = current.get("API_SERVER_CORS_ORIGINS", "").split(",")
    origins = [
        origin.strip().rstrip("/")
        for origin in [*admin_origins, *existing_origins]
        if origin.strip()
    ]
    if not origins:
        raise ValueError("At least one HM admin origin is required")
    write_env_values(
        env_path,
        {
            "API_SERVER_KEY": api_key,
            "API_SERVER_HOST": DEFAULT_API_HOST,
            "API_SERVER_PORT": str(port),
            "API_SERVER_CORS_ORIGINS": ",".join(dict.fromkeys(origins)),
            "HERMES_ACCEPT_HOOKS": "1",
        },
    )
    return api_key, f"http://{DEFAULT_API_HOST}:{port}"


def pairing_code(api_base_url: str, api_key: str, worker_id: str) -> str:
    normalized_worker_id = worker_id.strip()
    if not normalized_worker_id or len(normalized_worker_id) > 80:
        raise ValueError("HM_WORKER_ID must contain 1-80 characters")
    payload = json.dumps(
        {
            "apiBaseUrl": api_base_url,
            "apiKey": api_key,
            "workerId": normalized_worker_id,
        },
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("utf-8")
    encoded = base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
    return f"{PAIRING_PREFIX}{encoded}"


def remove_obsolete_jobs(hermes: str) -> None:
    listed = call([hermes, "cron", "list", "--all"])
    for name in obsolete_job_names(listed.stdout):
        call([hermes, "cron", "remove", name], allow_failure=True)


def stop_legacy_watchers(home: Path) -> int:
    """Stop only the old continuous ingest process during POSIX migration."""
    if os.name == "nt":
        return 0
    worker_path = str(
        (
            home
            / "skills"
            / "facebook-video-ingest"
            / "scripts"
            / "facebook_video_ingest.py"
        ).resolve()
    )
    completed = subprocess.run(
        ["ps", "-axo", "pid=,command="],
        text=True,
        encoding=SUBPROCESS_ENCODING,
        errors=SUBPROCESS_ERRORS,
        capture_output=True,
        check=False,
    )
    stopped = 0
    for line in completed.stdout.splitlines():
        pid_text, separator, command = line.strip().partition(" ")
        if not separator or not pid_text.isdigit():
            continue
        if worker_path not in command or "--watch" not in command:
            continue
        pid = int(pid_text)
        if pid == os.getpid():
            continue
        try:
            os.kill(pid, signal.SIGTERM)
            stopped += 1
        except (ProcessLookupError, PermissionError):
            continue
    return stopped


def api_is_ready(api_base_url: str, api_key: str, timeout_seconds: float = 15) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        outgoing = request.Request(
            f"{api_base_url}/api/jobs?include_disabled=true",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Accept": "application/json",
            },
        )
        try:
            with request.urlopen(outgoing, timeout=2) as response:
                if response.status == 200:
                    return True
        except (error.URLError, TimeoutError, OSError):
            time.sleep(0.5)
    return False


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    hermes = hermes_command()
    home = hermes_home()
    origins = args.admin_origins or list(DEFAULT_ADMIN_ORIGINS)
    env_values = parse_env_file(home / ".env")
    worker_id = env_values.get("HM_WORKER_ID", "").strip()
    if not worker_id:
        raise RuntimeError("HM_WORKER_ID is missing from ~/.hermes/.env")

    if args.check:
        api_key = env_values.get("API_SERVER_KEY", "")
        port_text = env_values.get("API_SERVER_PORT", str(args.api_port))
        port = int(port_text) if port_text.isdigit() else args.api_port
        api_base_url = f"http://{DEFAULT_API_HOST}:{port}"
        call([hermes, "gateway", "status"], allow_failure=True)
        if len(api_key) < 16 or not api_is_ready(api_base_url, api_key, 3):
            raise RuntimeError("Hermes local API is not configured or not reachable")
        print(f"Hermes local API ready: {api_base_url}")
        if args.show_pairing_code:
            print(
                f"HM Hermes pairing code:\n"
                f"{pairing_code(api_base_url, api_key, worker_id)}"
            )
        return 0

    install_runner(home)
    install_gateway_api_extension(home)
    ensure_dependencies(home)
    api_key, api_base_url = configure_api_server(
        home, port=args.api_port, admin_origins=origins
    )
    ensure_gateway(hermes)
    call([hermes, "gateway", "restart"])
    remove_obsolete_jobs(hermes)
    stopped = stop_legacy_watchers(home)
    if not api_is_ready(api_base_url, api_key):
        raise RuntimeError(f"Hermes local API did not become ready at {api_base_url}")
    print(f"Hermes local API ready: {api_base_url}")
    print("On-demand mode enabled; no continuous HM capture Worker is running.")
    if stopped:
        print(f"Stopped {stopped} legacy continuous Worker process(es).")
    if not args.no_pairing_code:
        print("Paste this code into 后台 → 视频抓取任务 → 连接本机 Hermes:")
        print(pairing_code(api_base_url, api_key, worker_id))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"Worker installation failed: {exc}", file=sys.stderr)
        raise SystemExit(2)
