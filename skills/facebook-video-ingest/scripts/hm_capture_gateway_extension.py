#!/usr/bin/env python3
"""Restricted Hermes API support for deterministic HM capture Cron jobs.

This module is installed beside ``gateway.platforms.api_server``.  The API
server calls :func:`prepare_capture_job_body` before creating a Cron job so an
authenticated HM browser can request a task-specific copy of the already
installed, trusted runner without gaining arbitrary filesystem write access.
"""

from __future__ import annotations

import os
import json
import importlib.util
import hmac
import re
import shutil
from pathlib import Path
from typing import Any


REQUEST_FIELD = "hm_capture_runner"
BASE_RUNNER_NAME = "hm_facebook_video_ingest_worker.py"
TASK_PATTERN = re.compile(r"^C-[A-Za-z0-9-]+$")
EXECUTION_PATTERN = re.compile(r"^E-[A-Za-z0-9-]+$")
SCHEDULE_KEY_FRAGMENT = (
    r"(?:\d{4}|LEGACY-\d{4}|[A-Z][A-Z0-9_]{0,31}-\d{4}-\d{1,3})"
)
SCHEDULE_KEY_PATTERN = re.compile(rf"^{SCHEDULE_KEY_FRAGMENT}$")
SCHEDULE_VERSION_PATTERN = re.compile(r"^[1-9]\d{0,9}$")
RUNNER_REVISION_PATTERN = re.compile(r"^r\d+$", re.IGNORECASE)
MANAGED_RUNNER_PATTERN = re.compile(
    rf"^(?:hm_capture_C-[A-Za-z0-9-]+_"
    rf"(?:E-[A-Za-z0-9-]+|(?:v[1-9]\d{{0,9}}_)?{SCHEDULE_KEY_FRAGMENT})"
    rf"(?:_r\d+)?|hm_capture_upload_C-[A-Za-z0-9-]+_V-[A-Za-z0-9-]+)\.py$",
    re.IGNORECASE,
)
VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v", ".webm", ".mkv"}


def _hermes_home() -> Path:
    try:
        from hermes_constants import get_hermes_home

        return Path(get_hermes_home()).expanduser()
    except (ImportError, OSError, TypeError, ValueError):
        return Path(
            os.getenv("HERMES_HOME", str(Path.home() / ".hermes"))
        ).expanduser()


def _normalized(value: Any, pattern: re.Pattern[str], label: str) -> str:
    text = str(value or "").strip().upper()
    if not pattern.fullmatch(text):
        raise ValueError(f"Invalid HM capture {label}")
    return text


def _materialize_runner(spec: dict[str, Any], home: Path) -> str:
    task_no = _normalized(spec.get("taskNo"), TASK_PATTERN, "task number")
    execution_value = str(spec.get("executionNo") or "").strip()
    schedule_value = str(spec.get("scheduleKey") or "").strip()
    schedule_version_value = str(spec.get("scheduleVersion") or "").strip()
    upload_value = str(spec.get("uploadVideoNo") or "").strip()
    revision_value = str(spec.get("runnerRevision") or "").strip().lower()
    if sum(bool(value) for value in (execution_value, schedule_value, upload_value)) != 1:
        raise ValueError(
            "HM capture runner requires exactly one executionNo, scheduleKey, or uploadVideoNo"
        )
    if schedule_version_value and not schedule_value:
        raise ValueError("HM capture schedule version requires scheduleKey")
    if execution_value:
        suffix = _normalized(
            execution_value, EXECUTION_PATTERN, "execution number"
        )
    elif schedule_value:
        schedule_key = _normalized(
            schedule_value, SCHEDULE_KEY_PATTERN, "schedule key"
        )
        schedule_version = (
            _normalized(
                schedule_version_value,
                SCHEDULE_VERSION_PATTERN,
                "schedule version",
            )
            if schedule_version_value
            else ""
        )
        suffix = (
            f"v{schedule_version}_{schedule_key}"
            if schedule_version
            else schedule_key
        )
    else:
        suffix = _normalized(
            upload_value,
            re.compile(r"^V-[A-Za-z0-9-]+$"),
            "video number",
        )
    revision_suffix = ""
    if revision_value:
        revision_suffix = "_" + _normalized(
            revision_value, RUNNER_REVISION_PATTERN, "runner revision"
        ).lower()
    if upload_value and revision_suffix:
        raise ValueError("HM upload runners do not accept a runner revision")

    scripts_dir = (home / "scripts").resolve()
    source = (scripts_dir / BASE_RUNNER_NAME).resolve()
    runner_name = (
        f"hm_capture_upload_{task_no}_{suffix}.py"
        if upload_value else f"hm_capture_{task_no}_{suffix}{revision_suffix}.py"
    )
    target = (scripts_dir / runner_name).resolve()
    try:
        source.relative_to(scripts_dir)
        target.relative_to(scripts_dir)
    except ValueError as exc:
        raise ValueError("HM capture runner path escaped scripts directory") from exc
    if not source.is_file():
        raise FileNotFoundError(f"HM capture base runner is missing: {source}")

    scripts_dir.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(f"{target.suffix}.tmp")
    try:
        shutil.copy2(source, temporary)
        os.replace(temporary, target)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    try:
        target.chmod(0o700)
    except OSError:
        pass
    return target.name


def prepare_capture_job_body(
    body: dict[str, Any], *, home: Path | None = None
) -> dict[str, Any]:
    """Materialize an HM runner and return a standard no-agent Cron body."""
    if not isinstance(body, dict):
        raise ValueError("Cron request body must be an object")
    prepared = dict(body)
    server_spec = prepared.pop("hm_server_runner", None)
    if server_spec is not None:
        root = (home or _hermes_home()).resolve()
        module_path = root / "skills/facebook-video-ingest/scripts/hm_server_worker.py"
        module_spec = importlib.util.spec_from_file_location("hm_server_worker", module_path)
        module = importlib.util.module_from_spec(module_spec)
        module_spec.loader.exec_module(module)
        server_spec = module.validate_spec(server_spec)
        tenant_prefix = server_spec.get("tenant", "")
        name = f'hm_server_{tenant_prefix + "_" if tenant_prefix else ""}{server_spec["dispatchId"]}_{server_spec["attempt"]}.py'
        scripts = root / "scripts"
        scripts.mkdir(parents=True, exist_ok=True)
        content = "import sys\nfrom pathlib import Path\nsys.path.insert(0, " + repr(str(module_path.parent)) + ")\nfrom hm_server_worker import launch\nlaunch(" + repr(server_spec) + ")\n"
        target = scripts / name
        temporary = target.with_suffix(".tmp")
        temporary.write_text(content, encoding="utf-8")
        os.replace(temporary, target)
        prepared.update(script=name, no_agent=True, skills=[])
        return prepared
    spec = prepared.pop(REQUEST_FIELD, None)
    if spec is None:
        return prepared
    if not isinstance(spec, dict):
        raise ValueError(f"{REQUEST_FIELD} must be an object")
    prepared["script"] = _materialize_runner(spec, (home or _hermes_home()))
    prepared["no_agent"] = True
    # A no-agent job executes only the trusted local script. Avoid retaining
    # misleading agent Skill attachments in the Hermes Cron UI.
    prepared["skills"] = []
    return prepared


def cleanup_capture_job_script(
    job: dict[str, Any] | None, *, home: Path | None = None
) -> None:
    """Remove only an HM-managed task-specific runner after Cron deletion."""
    if not isinstance(job, dict):
        return
    script = str(job.get("script") or "").strip()
    if not (MANAGED_RUNNER_PATTERN.fullmatch(script) or re.fullmatch(r"hm_server_(?:(?:ph|th|vn|id)_)?[1-9][0-9]*_[1-9][0-9]*\.py", script)):
        return
    scripts_dir = ((home or _hermes_home()) / "scripts").resolve()
    target = (scripts_dir / script).resolve()
    try:
        target.relative_to(scripts_dir)
    except ValueError:
        return
    try:
        target.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        # Windows may still hold the running script. The completed job cleanup
        # on the next install/sync can safely retry; never fail job deletion.
        pass


def regional_media_config(authorization: str) -> dict | None:
    registry = os.getenv("HM_TENANT_CONFIG")
    if not registry:
        return None
    token = authorization[7:].strip() if authorization.startswith("Bearer ") else ""
    for config in json.loads(Path(registry).read_text()).values():
        expected = config.get("mediaToken", "")
        if expected and hmac.compare_digest(token.encode(), expected.encode()):
            return config
    raise PermissionError("Regional media token is invalid")


def resolve_capture_video_path(value: Any, *, home: Path | None = None, authorization: str = "") -> Path:
    """Resolve only downloaded video files below configured HM media roots."""
    regional = regional_media_config(authorization)
    candidate = Path(str(value or "")).expanduser().resolve()
    if regional:
        try:
            candidate.relative_to(Path(regional["mediaRoot"]).resolve())
        except ValueError as error:
            raise PermissionError("Video belongs to another region") from error
    if candidate.suffix.lower() not in VIDEO_EXTENSIONS or not candidate.is_file():
        raise FileNotFoundError("HM capture video is missing")
    hermes_home = (home or _hermes_home()).resolve()
    roots = [
        Path(os.getenv("HM_INGEST_STATE_DIR", hermes_home / "facebook-video-ingest" / "executions")),
        Path(os.getenv("FACEBOOK_FOLLOWED_OUTPUT", Path.home() / "Desktop" / "Facebook")),
        Path(os.getenv("FB_FOLLOWED_DESKTOP", Path.home() / "Desktop" / "Facebook")),
    ]
    if regional:
        roots = [Path(regional["mediaRoot"])]
    for root in roots:
        try:
            candidate.relative_to(root.expanduser().resolve())
            return candidate
        except ValueError:
            continue
    raise PermissionError("HM capture video is outside configured media roots")


def delete_capture_video_path(value: Any, *, home: Path | None = None, authorization: str = "") -> Path:
    """Delete one explicitly requested video below configured HM media roots."""
    candidate = resolve_capture_video_path(value, home=home, authorization=authorization)
    candidate.unlink()
    return candidate
