#!/usr/bin/env python3
"""Update installed Operation Skills from an R2 release manifest."""

from __future__ import annotations

import argparse
import base64
import contextlib
import hashlib
import json
import ntpath
import os
import plistlib
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import IO, Any, Iterator, Optional


SCHEMA_VERSION = 1
REPOSITORY = "02030708dw/operation-skill"
PIPELINE_SKILLS = {
    "facebook-followed-video-download",
    "cloudflare-r2-video-upload",
    "facebook-video-ingest",
}
CORE_SKILLS = {
    "facebook-daily-like",
    "facebook-daily-comment",
    "facebook-followed-video-download",
    "cloudflare-r2-video-upload",
    "facebook-video-ingest",
}
EXCLUDED_DISCOVERY_PARTS = {
    ".hub",
    ".trash",
    "node_modules",
    "references",
    "scripts",
    "tests",
    "examples",
    "assets",
    "agents",
    "__pycache__",
}
HASH_IGNORED_PARTS = {"__pycache__", "node_modules"}
HASH_IGNORED_NAMES = {".DS_Store"}
NAME_PATTERN = re.compile(r'^name:\s*["\']?([^"\'\s]+)')
MAX_ARCHIVE_BYTES = 100 * 1024 * 1024
MAX_EXTRACTED_BYTES = 200 * 1024 * 1024
MAX_ARCHIVE_ENTRIES = 10_000
MAX_ARCHIVE_PATH_CHARS = 512
MAX_ARCHIVE_COMPONENT_CHARS = 255
LOG_LIMIT_BYTES = 1024 * 1024
BACKUP_RETENTION = 3
USER_AGENT = "HM-Operation-Skill-Updater/1.0"
TRANSACTION_SCHEMA_VERSION = 1
TRANSACTION_JOURNAL_NAME = "journal.json"


class UpdaterError(RuntimeError):
    pass


class UpdaterBusyError(UpdaterError):
    """Raised when another updater owns the shared state/executable lock."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def default_hermes_home() -> Path:
    configured = os.getenv("HERMES_HOME", "").strip()
    return Path(configured).expanduser() if configured else Path.home() / ".hermes"


def updater_home(hermes_home: Path) -> Path:
    return hermes_home / "operation-skill-updater"


def rotate_log(path: Path) -> None:
    try:
        if path.stat().st_size < LOG_LIMIT_BYTES:
            return
    except OSError:
        return
    previous = path.with_suffix(path.suffix + ".1")
    with contextlib.suppress(OSError):
        previous.unlink()
    with contextlib.suppress(OSError):
        path.replace(previous)


def log_message(home: Path, message: str) -> None:
    log_path = updater_home(home) / "logs" / "updater.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    rotate_log(log_path)
    line = f"{utc_now()} {message}\n"
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(line)
    print(message)


def atomic_json_write(path: Path, payload: dict[str, Any]) -> None:
    """Backward-compatible alias used by older callers and test fixtures."""
    durable_json_write(path, payload)


def fsync_directory(path: Path) -> None:
    """Persist directory-entry changes where the platform supports it."""
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def durable_replace(source: Path, destination: Path) -> None:
    """Atomically replace a path and durably commit the rename."""
    if os.name == "nt":
        # os.replace() maps to MoveFileExW without MOVEFILE_WRITE_THROUGH.
        # The write-through flag is required here so a hard power loss cannot
        # reorder the acceptedRelease rename after the updater replacement.
        import ctypes

        move_file_ex = ctypes.WinDLL("kernel32", use_last_error=True).MoveFileExW
        move_file_ex.argtypes = [
            ctypes.c_wchar_p,
            ctypes.c_wchar_p,
            ctypes.c_uint32,
        ]
        move_file_ex.restype = ctypes.c_int
        MOVEFILE_REPLACE_EXISTING = 0x1
        MOVEFILE_WRITE_THROUGH = 0x8
        flags = MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH
        if not move_file_ex(str(source), str(destination), flags):
            raise ctypes.WinError(ctypes.get_last_error())
        return
    os.replace(source, destination)
    fsync_directory(destination.parent)


def durable_json_write(path: Path, payload: dict[str, Any]) -> None:
    """Atomically write a journal and flush both its contents and directory entry."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        durable_replace(temporary, path)
    finally:
        with contextlib.suppress(OSError):
            temporary.unlink()


def create_json_exclusive(path: Path, payload: dict[str, Any]) -> bool:
    """Publish a complete JSON file without ever replacing an existing path.

    The payload is durably written to a private temporary inode first.  A hard
    link then acts as the atomic, no-clobber compare-and-swap.  In particular,
    a crash can leave an unreferenced temporary file, but can never expose an
    empty or partially-written ESTOP sentinel.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    descriptor = os.open(temporary, flags, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            return False
        except OSError as exc:
            raise UpdaterError(f"无法原子创建 {path}: {exc}") from exc
        fsync_directory(path.parent)
        return True
    finally:
        with contextlib.suppress(OSError):
            temporary.unlink()


def load_json(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return dict(default)
    except (OSError, json.JSONDecodeError) as exc:
        raise UpdaterError(f"无法读取 {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise UpdaterError(f"JSON 根节点必须是对象: {path}")
    return payload


def state_path(home: Path) -> Path:
    return updater_home(home) / "state.json"


def config_path(home: Path) -> Path:
    return updater_home(home) / "config.json"


def load_state(home: Path) -> dict[str, Any]:
    return load_json(state_path(home), {"schemaVersion": 1, "skills": {}})


def validate_skill_names(value: Any, label: str) -> set[str]:
    if value is None:
        return set()
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise UpdaterError(f"{label} 必须是 Skill 名称数组")
    names = set(value)
    invalid = sorted(name for name in names if not re.fullmatch(r"[a-z][a-z0-9-]*", name))
    if invalid:
        raise UpdaterError(f"{label} 包含无效 Skill 名称: {', '.join(invalid)}")
    return names


def load_managed_skills(home: Path) -> set[str]:
    config = load_json(config_path(home), {})
    return validate_skill_names(config.get("managedSkills"), "config managedSkills")


def persist_managed_skills(home: Path, names: set[str]) -> set[str]:
    config = load_json(config_path(home), {})
    managed = validate_skill_names(config.get("managedSkills"), "config managedSkills")
    managed.update(names)
    config["managedSkills"] = sorted(managed)
    durable_json_write(config_path(home), config)
    return managed


def persist_managed_skills_locked(home: Path, names: set[str]) -> set[str]:
    """Merge durable install intent without racing bootstrap config writes."""
    with updater_lock(home):
        return persist_managed_skills(home, names)


def resolve_manifest_url(home: Path, override: str) -> str:
    if override.strip():
        return override.strip()
    config = load_json(config_path(home), {})
    value = str(config.get("manifestUrl", "")).strip()
    if not value:
        raise UpdaterError(
            f"缺少 manifest URL；请重新运行安装脚本或写入 {config_path(home)}"
        )
    return value


def validate_url(url: str) -> None:
    parsed = urllib.parse.urlparse(url)
    allowed = {"https"}
    if os.getenv("OPERATION_SKILL_UPDATER_ALLOW_FILE_URL") == "1":
        allowed.add("file")
    if parsed.scheme not in allowed:
        raise UpdaterError(f"只允许 HTTPS 发布地址: {url}")


def request_bytes(url: str, *, limit: int, expected_size: Optional[int] = None) -> bytes:
    validate_url(url)
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            validate_url(response.geturl())
            content_length = response.headers.get("Content-Length")
            if content_length and int(content_length) > limit:
                raise UpdaterError(f"下载内容超过限制: {url}")
            payload = response.read(limit + 1)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise UpdaterError(f"下载失败 {url}: {exc}") from exc
    if len(payload) > limit:
        raise UpdaterError(f"下载内容超过限制: {url}")
    if expected_size is not None and len(payload) != expected_size:
        raise UpdaterError(
            f"下载大小不一致: expected={expected_size}, actual={len(payload)}"
        )
    return payload


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def included_hash_file(path: Path, root: Path) -> bool:
    relative = path.relative_to(root)
    if path.name in HASH_IGNORED_NAMES or path.suffix == ".pyc":
        return False
    return not any(part in HASH_IGNORED_PARTS for part in relative.parts)


def directory_hash(skill_dir: Path) -> str:
    symlinks = sorted(path for path in skill_dir.rglob("*") if path.is_symlink())
    if symlinks:
        relative = symlinks[0].relative_to(skill_dir)
        raise UpdaterError(f"Skill 不允许符号链接: {skill_dir.name}/{relative}")
    digest = hashlib.sha256()
    paths = sorted(
        (
            path
            for path in skill_dir.rglob("*")
            if path.is_file() and included_hash_file(path, skill_dir)
        ),
        # Path ordering is case-insensitive on Windows. Match the builder's
        # case-sensitive POSIX relative strings on every platform.
        key=lambda path: path.relative_to(skill_dir).as_posix(),
    )
    for path in paths:
        relative = path.relative_to(skill_dir).as_posix().encode("utf-8")
        content = path.read_bytes()
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def skill_name(skill_md: Path) -> str:
    try:
        lines = skill_md.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise UpdaterError(f"无法读取 {skill_md}: {exc}") from exc
    if not lines or lines[0].strip() != "---":
        raise UpdaterError(f"Skill 缺少 YAML frontmatter: {skill_md}")
    for line in lines[1:]:
        if line.strip() == "---":
            break
        match = NAME_PATTERN.match(line)
        if match:
            return match.group(1)
    raise UpdaterError(f"Skill 缺少 name: {skill_md}")


def find_installed_skills(skills_root: Path, managed_names: set[str]) -> dict[str, Path]:
    found: dict[str, Path] = {}
    if not skills_root.is_dir():
        return found
    for skill_md in skills_root.rglob("SKILL.md"):
        relative = skill_md.relative_to(skills_root)
        if any(part.startswith(".") or part in EXCLUDED_DISCOVERY_PARTS for part in relative.parts[:-1]):
            continue
        name = skill_name(skill_md)
        if name not in managed_names:
            continue
        directory = skill_md.parent.resolve()
        previous = found.get(name)
        if previous and previous != directory:
            raise UpdaterError(f"发现重复安装的 Skill {name}: {previous}, {directory}")
        found[name] = directory
    return found


def validate_manifest(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise UpdaterError("manifest 根节点必须是对象")
    if payload.get("schemaVersion") != SCHEMA_VERSION:
        raise UpdaterError(f"不支持的 manifest schema: {payload.get('schemaVersion')}")
    if payload.get("repository") != REPOSITORY or payload.get("channel") != "main":
        raise UpdaterError("manifest 来源或通道不匹配")
    commit = str(payload.get("commit", ""))
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise UpdaterError("manifest commit 无效")
    release_sequence = payload.get("releaseSequence")
    if (
        not isinstance(release_sequence, int)
        or isinstance(release_sequence, bool)
        or release_sequence <= 0
    ):
        raise UpdaterError("manifest releaseSequence 无效")
    archive = payload.get("archive")
    if not isinstance(archive, dict):
        raise UpdaterError("manifest archive 无效")
    validate_url(str(archive.get("url", "")))
    if not re.fullmatch(r"[0-9a-f]{64}", str(archive.get("sha256", ""))):
        raise UpdaterError("manifest archive SHA-256 无效")
    size = archive.get("size")
    if not isinstance(size, int) or size <= 0 or size > MAX_ARCHIVE_BYTES:
        raise UpdaterError("manifest archive 大小无效")
    skills = payload.get("skills")
    if not isinstance(skills, list) or not skills:
        raise UpdaterError("manifest skills 为空")
    names: set[str] = set()
    for entry in skills:
        if not isinstance(entry, dict):
            raise UpdaterError("manifest skill 条目无效")
        name = str(entry.get("name", ""))
        if not re.fullmatch(r"[a-z][a-z0-9-]*", name) or name in names:
            raise UpdaterError(f"manifest Skill 名称无效或重复: {name}")
        names.add(name)
        if entry.get("path") != f"skills/{name}":
            raise UpdaterError(f"manifest Skill 路径无效: {name}")
        if not re.fullmatch(r"[0-9a-f]{64}", str(entry.get("sha256", ""))):
            raise UpdaterError(f"manifest Skill SHA-256 无效: {name}")
    updater = payload.get("updater")
    if updater is not None:
        if not isinstance(updater, dict):
            raise UpdaterError("manifest updater 无效")
        validate_url(str(updater.get("url", "")))
        if not re.fullmatch(r"[0-9a-f]{64}", str(updater.get("sha256", ""))):
            raise UpdaterError("manifest updater SHA-256 无效")
        updater_size = updater.get("size")
        if (
            not isinstance(updater_size, int)
            or isinstance(updater_size, bool)
            or updater_size <= 0
            or updater_size > 2 * 1024 * 1024
        ):
            raise UpdaterError("manifest updater 大小无效")
    return payload


def fetch_manifest(url: str) -> dict[str, Any]:
    payload = request_bytes(url, limit=1024 * 1024)
    try:
        decoded = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise UpdaterError(f"manifest JSON 无效: {exc}") from exc
    return validate_manifest(decoded)


def validate_release_progress(manifest: dict[str, Any], state: dict[str, Any]) -> None:
    accepted = state.get("acceptedRelease")
    if accepted is None:
        return
    if not isinstance(accepted, dict):
        raise UpdaterError("本地 acceptedRelease 状态无效")
    accepted_sequence = accepted.get("releaseSequence")
    accepted_commit = str(accepted.get("commit", ""))
    if (
        not isinstance(accepted_sequence, int)
        or isinstance(accepted_sequence, bool)
        or accepted_sequence <= 0
        or not re.fullmatch(r"[0-9a-f]{40}", accepted_commit)
    ):
        raise UpdaterError("本地 acceptedRelease 状态无效")
    candidate_sequence = manifest["releaseSequence"]
    candidate_commit = manifest["commit"]
    if candidate_sequence < accepted_sequence:
        raise UpdaterError(
            "拒绝回退到旧发布序列: "
            f"accepted={accepted_sequence}, candidate={candidate_sequence}"
        )
    if candidate_sequence == accepted_sequence and candidate_commit != accepted_commit:
        raise UpdaterError(
            "同一发布序列对应不同 commit，已拒绝更新: "
            f"sequence={candidate_sequence}"
        )


def record_accepted_release(state: dict[str, Any], manifest: dict[str, Any]) -> None:
    previous = state.get("acceptedRelease")
    if (
        isinstance(previous, dict)
        and previous.get("releaseSequence") == manifest["releaseSequence"]
        and previous.get("commit") == manifest["commit"]
    ):
        return
    state["acceptedRelease"] = {
        "releaseSequence": manifest["releaseSequence"],
        "commit": manifest["commit"],
        "acceptedAt": utc_now(),
    }


def record_skill_versions(
    state: dict[str, Any],
    manifest: dict[str, Any],
    statuses: list[dict[str, Any]],
    changed: list[str],
) -> None:
    remote_entries = {entry["name"]: entry for entry in manifest["skills"]}
    recorded = state.get("skills") if isinstance(state.get("skills"), dict) else {}
    state["skills"] = recorded
    for item in statuses:
        if item["name"] in changed or item["status"] == "up_to_date":
            entry = remote_entries[item["name"]]
            recorded[item["name"]] = {
                "sha256": entry["sha256"],
                "version": entry.get("version", ""),
                "path": item["path"],
                "commit": manifest["commit"],
                "recordedAt": utc_now(),
            }


def record_adoption(
    state: dict[str, Any],
    manifest: dict[str, Any],
    names: list[str],
    backup: str,
) -> None:
    if not names:
        return
    history = state.get("adoptions")
    if not isinstance(history, list):
        history = []
        state["adoptions"] = history
    identity = (manifest["releaseSequence"], manifest["commit"], tuple(sorted(names)))
    for item in history:
        if not isinstance(item, dict):
            continue
        existing = (
            item.get("releaseSequence"),
            item.get("commit"),
            tuple(sorted(item.get("skills", []))) if isinstance(item.get("skills"), list) else (),
        )
        if existing == identity:
            return
    history.append(
        {
            "releaseSequence": manifest["releaseSequence"],
            "commit": manifest["commit"],
            "skills": sorted(names),
            "backup": backup,
            "adoptedAt": utc_now(),
        }
    )


def compute_statuses(
    manifest: dict[str, Any], installed: dict[str, Path], state: dict[str, Any]
) -> list[dict[str, Any]]:
    remote = {entry["name"]: entry for entry in manifest["skills"]}
    recorded = state.get("skills", {}) if isinstance(state.get("skills"), dict) else {}
    statuses: list[dict[str, Any]] = []
    for name, path in sorted(installed.items()):
        entry = remote[name]
        current_hash = directory_hash(path)
        desired_hash = entry["sha256"]
        has_baseline = name in recorded and isinstance(recorded.get(name), dict)
        previous = recorded.get(name) if has_baseline else {}
        previous_hash = str(previous.get("sha256", ""))
        if current_hash == desired_hash:
            status = "up_to_date"
        elif not has_baseline:
            status = "unmanaged_existing"
        elif not re.fullmatch(r"[0-9a-f]{64}", previous_hash):
            status = "local_modified"
        elif current_hash != previous_hash:
            status = "local_modified"
        else:
            status = "update_available"
        statuses.append(
            {
                "name": name,
                "path": str(path),
                "status": status,
                "currentSha256": current_hash,
                "desiredSha256": desired_hash,
                "version": entry.get("version", ""),
                "hadBaseline": has_baseline,
            }
        )
    return statuses


def safe_extract(archive_path: Path, target: Path) -> None:
    seen: set[str] = set()
    extracted_bytes = 0
    with zipfile.ZipFile(archive_path) as archive:
        entries = archive.infolist()
        if len(entries) > MAX_ARCHIVE_ENTRIES:
            raise UpdaterError("ZIP 文件条目数量超过限制")
        for info in entries:
            raw = info.filename.replace("\\", "/")
            pure = PurePosixPath(raw)
            if not raw or pure.is_absolute() or ".." in pure.parts:
                raise UpdaterError(f"ZIP 包含不安全路径: {raw}")
            if len(raw) > MAX_ARCHIVE_PATH_CHARS or any(
                len(part) > MAX_ARCHIVE_COMPONENT_CHARS for part in pure.parts
            ):
                raise UpdaterError(f"ZIP 路径过长: {raw[:120]}")
            if not pure.parts or pure.parts[0] != "skills":
                raise UpdaterError(f"ZIP 包含非 Skill 路径: {raw}")
            folded = raw.casefold()
            if folded in seen:
                raise UpdaterError(f"ZIP 包含重复路径: {raw}")
            seen.add(folded)
            mode = (info.external_attr >> 16) & 0xFFFF
            if stat.S_ISLNK(mode):
                raise UpdaterError(f"ZIP 不允许符号链接: {raw}")
            extracted_bytes += info.file_size
            if extracted_bytes > MAX_EXTRACTED_BYTES:
                raise UpdaterError("ZIP 解压后内容超过限制")
            destination = target.joinpath(*pure.parts)
            destination.resolve().relative_to(target.resolve())
            if info.is_dir():
                destination.mkdir(parents=True, exist_ok=True)
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as source, destination.open("wb") as output:
                shutil.copyfileobj(source, output)
            if mode & 0o111 and os.name != "nt":
                destination.chmod(0o755)


def validate_staging(target: Path, manifest: dict[str, Any]) -> None:
    for entry in manifest["skills"]:
        skill_dir = target / entry["path"]
        skill_md = skill_dir / "SKILL.md"
        if not skill_md.is_file() or skill_name(skill_md) != entry["name"]:
            raise UpdaterError(f"发布包 Skill 结构无效: {entry['name']}")
        actual = directory_hash(skill_dir)
        if actual != entry["sha256"]:
            raise UpdaterError(
                f"发布包 Skill 校验失败: {entry['name']} expected={entry['sha256']} actual={actual}"
            )


def try_lock(path: Path) -> Optional[IO[bytes]]:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    try:
        if os.name == "nt":
            import msvcrt

            if path.stat().st_size == 0:
                handle.write(b"0")
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


@contextlib.contextmanager
def updater_lock(home: Path) -> Iterator[None]:
    path = updater_home(home) / "updater.lock"
    handle = try_lock(path)
    if handle is None:
        raise UpdaterBusyError("另一个 Skill 更新进程正在运行")
    try:
        yield
    finally:
        handle.close()


def active_skill_processes(paths: list[Path]) -> list[str]:
    needles = [str(path.resolve()).lower() for path in paths]
    if not needles:
        return []
    lines: list[tuple[int, str]] = []
    try:
        if os.name == "nt":
            command = (
                "Get-CimInstance Win32_Process | Select-Object ProcessId,CommandLine | "
                "ConvertTo-Json -Compress"
            )
            completed = subprocess.run(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
                text=True,
                capture_output=True,
                check=False,
                timeout=20,
            )
            if completed.returncode != 0:
                return ["process-inspection-unavailable"]
            payload = json.loads(completed.stdout or "[]")
            rows = payload if isinstance(payload, list) else [payload]
            lines = [
                (int(row.get("ProcessId", 0)), str(row.get("CommandLine") or ""))
                for row in rows
                if isinstance(row, dict)
            ]
        else:
            completed = subprocess.run(
                ["ps", "-axo", "pid=,command="],
                text=True,
                capture_output=True,
                check=False,
                timeout=20,
            )
            if completed.returncode != 0:
                return ["process-inspection-unavailable"]
            for raw in completed.stdout.splitlines():
                pid_text, separator, command = raw.strip().partition(" ")
                if separator and pid_text.isdigit():
                    lines.append((int(pid_text), command))
    except (OSError, subprocess.SubprocessError, ValueError, json.JSONDecodeError):
        return ["process-inspection-unavailable"]
    current = {os.getpid(), os.getppid()}
    return [command for pid, command in lines if pid not in current and any(item in command.lower() for item in needles)]


def downloader_lock_path(home: Path) -> Path:
    configured = (
        os.getenv("FACEBOOK_FOLLOWED_STATE_DIR", "").strip()
        or os.getenv("FB_FOLLOWED_STATE_DIR", "").strip()
    )
    state_dir = Path(configured).expanduser() if configured else home / "facebook-followed-video-download"
    return state_dir / ".capture-run.lock"


def acquire_worker_locks(home: Path) -> Optional[list[IO[bytes]]]:
    lock_dir = home / "facebook-video-ingest"
    handles: list[IO[bytes]] = []
    paths = {downloader_lock_path(home)}
    if lock_dir.is_dir():
        paths.update(lock_dir.glob("*.lock"))
    for path in sorted(paths):
        handle = try_lock(path)
        if handle is None:
            for acquired in handles:
                acquired.close()
            return None
        handles.append(handle)
    return handles


def owned_sentinel_owner(path: Path, reasons: set[str]) -> str:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return ""
    if not isinstance(payload, dict) or payload.get("reason") not in reasons:
        return ""
    owner = str(payload.get("owner", ""))
    return owner if re.fullmatch(r"[0-9a-f]{32}", owner) else ""


def owned_update_sentinel_owner(home: Path) -> str:
    return owned_sentinel_owner(home / "ESTOP", {"operation-skill-update"})


def owned_bridge_sentinel_owner(home: Path) -> str:
    return owned_sentinel_owner(
        home / "ESTOP",
        {"operation-skill-update", "operation-skill-bridge-repair"},
    )


def remove_owned_update_sentinel(home: Path, owner: str) -> bool:
    if not re.fullmatch(r"[0-9a-f]{32}", owner):
        return False
    path = home / "ESTOP"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return False
    if (
        not isinstance(payload, dict)
        or payload.get("reason") != "operation-skill-update"
        or payload.get("owner") != owner
    ):
        return False
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    return True


def remove_owned_repair_sentinel(path: Path, owner: str) -> bool:
    if not re.fullmatch(r"[0-9a-f]{32}", owner):
        return False
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return False
    if (
        not isinstance(payload, dict)
        or payload.get("reason")
        not in {"operation-skill-update", "operation-skill-bridge-repair"}
        or payload.get("owner") != owner
    ):
        return False
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    return True


class UpdatePause:
    def __init__(self, path: Path, token: str, owns_sentinel: bool) -> None:
        self.path = path
        self.token = token
        self.owns_sentinel = owns_sentinel
        self.idle = False
        self.retain_sentinel = False

    def __bool__(self) -> bool:
        return self.idle

    def retain_for_bridge_repair(self, repair: dict[str, Any]) -> str:
        del repair
        if not self.owns_sentinel:
            return ""
        self.retain_sentinel = True
        return self.token

    def release_after_bridge_repair(self, owner: str) -> None:
        if self.owns_sentinel and owner == self.token:
            self.retain_sentinel = False
            return
        remove_owned_repair_sentinel(self.path, owner)

    def cancel_retention(self) -> None:
        self.retain_sentinel = False

    def retain_for_recovery(self) -> None:
        if self.owns_sentinel:
            self.retain_sentinel = True

    def close(self) -> None:
        if not self.owns_sentinel:
            return
        if self.retain_sentinel:
            return
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            if (
                isinstance(payload, dict)
                and payload.get("owner") == self.token
                and payload.get("reason")
                in {"operation-skill-update", "operation-skill-bridge-repair"}
            ):
                self.path.unlink()
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            pass


@contextlib.contextmanager
def paused_for_update(
    home: Path, installed_paths: list[Path], timeout_seconds: int
) -> Iterator[UpdatePause]:
    sentinel = home / "ESTOP"
    token = uuid.uuid4().hex
    owns_sentinel = create_json_exclusive(
        sentinel,
        {"reason": "operation-skill-update", "engaged_at": utc_now(), "owner": token},
    )
    pause = UpdatePause(sentinel, token, owns_sentinel)
    worker_handles: list[IO[bytes]] = []
    idle = False
    deadline = time.monotonic() + max(0, timeout_seconds)
    try:
        while True:
            processes = active_skill_processes(installed_paths)
            handles = acquire_worker_locks(home)
            processes_after_lock = (
                active_skill_processes(installed_paths)
                if not processes and handles is not None
                else processes
            )
            if not processes and not processes_after_lock and handles is not None:
                worker_handles = handles
                idle = True
                break
            if handles is not None:
                for handle in handles:
                    handle.close()
            if time.monotonic() >= deadline:
                break
            time.sleep(min(5, max(0.1, deadline - time.monotonic())))
        pause.idle = idle
        yield pause
    finally:
        for handle in worker_handles:
            handle.close()
        pause.close()


def backup_skills(home: Path, targets: list[dict[str, Any]]) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    backup_root = updater_home(home) / "backups" / stamp
    copy_skill_backup(home, targets, backup_root)
    snapshots = sorted(path for path in backup_root.parent.iterdir() if path.is_dir())
    for old in snapshots[:-BACKUP_RETENTION]:
        shutil.rmtree(old, ignore_errors=True)
    return backup_root


def backup_adopted_skills(home: Path, targets: list[dict[str, Any]]) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    backup_root = updater_home(home) / "adoption-backups" / stamp
    copy_skill_backup(home, targets, backup_root)
    return backup_root


def copy_skill_backup(
    home: Path, targets: list[dict[str, Any]], backup_root: Path
) -> None:
    backup_root.mkdir(parents=True, exist_ok=True)
    skills_root = (home / "skills").resolve()
    for item in targets:
        source = Path(item["path"]).resolve()
        if not source.exists():
            continue
        relative = source.relative_to(skills_root)
        destination = backup_root / "skills" / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source, destination)


def validate_targets_unchanged(targets: list[dict[str, Any]]) -> None:
    for item in targets:
        path = Path(item["path"])
        expected = str(item.get("currentSha256", ""))
        if not expected:
            if path.exists():
                raise UpdaterError(f"Skill 在更新期间出现，已停止覆盖: {item['name']}")
            continue
        if not path.is_dir():
            raise UpdaterError(f"Skill 在更新期间被移动或删除，已停止覆盖: {item['name']}")
        actual = directory_hash(path)
        if actual != expected:
            raise UpdaterError(f"Skill 在更新期间发生本地修改，已停止覆盖: {item['name']}")


def find_npm(home: Path) -> Optional[str]:
    names = ("npm.cmd", "npm") if os.name == "nt" else ("npm",)
    directories = (
        home / "node" / "bin",
        home / "node",
        Path.home() / ".local" / "bin",
        Path("/opt/homebrew/bin"),
        Path("/usr/local/bin"),
    )
    for directory in directories:
        for name in names:
            candidate = directory / name
            if candidate.is_file() and (os.name == "nt" or os.access(candidate, os.X_OK)):
                return str(candidate)
    return shutil.which("npm.cmd") or shutil.which("npm")


def prepare_runtime_dependencies(
    home: Path, staging: Path, targets: list[dict[str, Any]]
) -> None:
    if not any(item["name"] == "facebook-followed-video-download" for item in targets):
        return
    scripts_dir = staging / "skills" / "facebook-followed-video-download" / "scripts"
    package_json = scripts_dir / "package.json"
    package_lock = scripts_dir / "package-lock.json"
    if not package_json.is_file() or not package_lock.is_file():
        raise UpdaterError("Facebook 下载 Skill 缺少 package.json 或 package-lock.json")
    npm = find_npm(home)
    if not npm:
        raise UpdaterError("更新 Facebook 下载 Skill 需要 npm，但当前电脑未找到 npm")
    path_entries = [str(Path(npm).parent), str(home / "node" / "bin"), str(home / "node")]
    existing_path = os.environ.get("PATH", "")
    if existing_path:
        path_entries.append(existing_path)
    runtime_env = os.environ.copy()
    runtime_env["PATH"] = os.pathsep.join(dict.fromkeys(path_entries))
    completed = subprocess.run(
        [npm, "ci", "--ignore-scripts", "--no-audit", "--no-fund"],
        cwd=scripts_dir,
        env=runtime_env,
        text=True,
        capture_output=True,
        check=False,
        timeout=300,
    )
    if completed.returncode:
        raise UpdaterError(
            "Facebook 下载 Skill 依赖安装失败 "
            f"(npm ci exit={completed.returncode})"
        )


def transaction_root(home: Path) -> Path:
    return updater_home(home) / "transactions"


def json_clone(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False))


def validate_transaction_destination(home: Path, name: str, raw_path: str) -> Path:
    if not re.fullmatch(r"[a-z][a-z0-9-]*", name):
        raise UpdaterError(f"事务 journal 包含无效 Skill 名称: {name}")
    candidate = Path(raw_path)
    if not candidate.is_absolute():
        raise UpdaterError(f"事务 journal Skill 路径必须是绝对路径: {name}")
    destination = candidate.resolve()
    skills_root = (home / "skills").resolve()
    try:
        relative = destination.relative_to(skills_root)
    except ValueError as exc:
        raise UpdaterError(f"事务 journal Skill 路径越界: {name}") from exc
    if not relative.parts or destination.name != name:
        raise UpdaterError(f"事务 journal Skill 路径与名称不匹配: {name}")
    return destination


def validate_transaction_journal(
    home: Path, rollback_root: Path, payload: dict[str, Any]
) -> list[dict[str, Any]]:
    if payload.get("schemaVersion") != TRANSACTION_SCHEMA_VERSION:
        raise UpdaterError(f"事务 journal schema 无效: {rollback_root}")
    if payload.get("kind") != "operation-skill-update":
        raise UpdaterError(f"事务 journal 类型无效: {rollback_root}")
    if payload.get("transactionId") != rollback_root.name:
        raise UpdaterError(f"事务 journal ID 不匹配: {rollback_root}")
    raw_targets = payload.get("targets")
    if not isinstance(raw_targets, list) or not raw_targets:
        raise UpdaterError(f"事务 journal targets 无效: {rollback_root}")

    validated: list[dict[str, Any]] = []
    names: set[str] = set()
    paths: set[str] = set()
    for raw in raw_targets:
        if not isinstance(raw, dict):
            raise UpdaterError(f"事务 journal target 无效: {rollback_root}")
        name = str(raw.get("name", ""))
        raw_path = raw.get("path")
        if not isinstance(raw_path, str):
            raise UpdaterError(f"事务 journal Skill 路径无效: {name}")
        destination = validate_transaction_destination(home, name, raw_path)
        folded_path = os.path.normcase(str(destination))
        if name in names or folded_path in paths:
            raise UpdaterError(f"事务 journal target 重复: {name}")
        names.add(name)
        paths.add(folded_path)

        existed = raw.get("existed")
        had_state = raw.get("hadState")
        desired_hash = str(raw.get("desiredSha256", ""))
        old_hash = str(raw.get("oldSha256", ""))
        if not isinstance(existed, bool) or not isinstance(had_state, bool):
            raise UpdaterError(f"事务 journal target 标志无效: {name}")
        if not re.fullmatch(r"[0-9a-f]{64}", desired_hash):
            raise UpdaterError(f"事务 journal 新版本 SHA-256 无效: {name}")
        if existed:
            if not re.fullmatch(r"[0-9a-f]{64}", old_hash):
                raise UpdaterError(f"事务 journal 旧版本 SHA-256 无效: {name}")
        elif old_hash:
            raise UpdaterError(f"事务 journal fresh target 不应包含旧哈希: {name}")
        if not had_state and raw.get("oldState") is not None:
            raise UpdaterError(f"事务 journal fresh state 无效: {name}")

        validated.append(
            {
                "name": name,
                "path": destination,
                "rollback": rollback_root / name,
                "existed": existed,
                "hadState": had_state,
                "oldState": json_clone(raw.get("oldState")),
                "oldSha256": old_hash,
                "desiredSha256": desired_hash,
            }
        )
    return validated


def checked_directory_hash(path: Path, description: str) -> Optional[str]:
    if path.is_symlink():
        raise UpdaterError(f"{description} 不允许符号链接: {path}")
    if not path.exists():
        return None
    if not path.is_dir():
        raise UpdaterError(f"{description} 不是目录: {path}")
    return directory_hash(path)


def remove_tree_checked(path: Path, description: str) -> None:
    if path.is_symlink() or not path.is_dir():
        raise UpdaterError(f"{description} 不是安全目录: {path}")
    try:
        shutil.rmtree(path)
    except OSError as exc:
        raise UpdaterError(f"无法删除{description}: {path}: {exc}") from exc
    if path.exists() or path.is_symlink():
        raise UpdaterError(f"无法删除{description}: {path}")


def cleanup_transaction(
    rollback_root: Path,
    journal_path: Path,
    targets: list[dict[str, Any]],
) -> None:
    """Delete rollback data first and the recovery journal last."""
    for item in targets:
        rollback = item["rollback"]
        if rollback.exists() or rollback.is_symlink():
            remove_tree_checked(rollback, "事务 rollback")

    unexpected = [path for path in rollback_root.iterdir() if path != journal_path]
    if unexpected:
        raise UpdaterError(f"事务目录包含未知内容，拒绝清理: {unexpected[0]}")
    try:
        journal_path.unlink()
    except OSError as exc:
        raise UpdaterError(f"无法清理事务 journal: {journal_path}: {exc}") from exc
    fsync_directory(rollback_root)
    with contextlib.suppress(OSError):
        rollback_root.rmdir()
    with contextlib.suppress(OSError):
        fsync_directory(rollback_root.parent)


def recover_transaction_directory(
    home: Path, rollback_root: Path, *, force_rollback: bool = False
) -> str:
    journal_path = rollback_root / TRANSACTION_JOURNAL_NAME
    if journal_path.is_symlink():
        raise UpdaterError(f"事务 journal 不允许符号链接: {journal_path}")
    payload = load_json(journal_path, {})
    targets = validate_transaction_journal(home, rollback_root, payload)
    state = load_state(home)
    skills_state = state.get("skills") if isinstance(state.get("skills"), dict) else {}

    current_hashes: dict[str, Optional[str]] = {}
    rollback_hashes: dict[str, Optional[str]] = {}
    for item in targets:
        name = item["name"]
        current_hashes[name] = checked_directory_hash(
            item["path"], f"事务目标 {name}"
        )
        rollback_hashes[name] = checked_directory_hash(
            item["rollback"], f"事务 rollback {name}"
        )

    committed = not force_rollback and all(
        isinstance(skills_state.get(item["name"]), dict)
        and skills_state[item["name"]].get("sha256") == item["desiredSha256"]
        and current_hashes[item["name"]] == item["desiredSha256"]
        for item in targets
    )
    if committed:
        cleanup_transaction(rollback_root, journal_path, targets)
        return "committed"

    # Validate every target before mutating any of them. This prevents a forged or
    # externally modified journal from turning recovery into an arbitrary delete.
    for item in targets:
        name = item["name"]
        current_hash = current_hashes[name]
        rollback_hash = rollback_hashes[name]
        if item["existed"]:
            if rollback_hash is not None:
                if rollback_hash != item["oldSha256"]:
                    raise UpdaterError(f"事务 rollback 内容校验失败: {name}")
                if current_hash not in (None, item["desiredSha256"]):
                    raise UpdaterError(f"事务目标内容未知，拒绝覆盖: {name}")
            elif current_hash != item["oldSha256"]:
                raise UpdaterError(f"事务旧版本缺失，无法安全恢复: {name}")
        else:
            if rollback_hash is not None:
                raise UpdaterError(f"fresh target 出现未知 rollback: {name}")
            if current_hash not in (None, item["desiredSha256"]):
                raise UpdaterError(f"fresh target 内容未知，拒绝删除: {name}")

    for item in reversed(targets):
        name = item["name"]
        destination = item["path"]
        rollback = item["rollback"]
        if item["existed"]:
            if rollback.exists():
                if destination.exists():
                    remove_tree_checked(destination, f"事务中新版本 Skill {name}")
                destination.parent.mkdir(parents=True, exist_ok=True)
                try:
                    os.replace(rollback, destination)
                except OSError as exc:
                    raise UpdaterError(f"无法恢复旧版本 Skill {name}: {exc}") from exc
                if checked_directory_hash(destination, f"已恢复 Skill {name}") != item["oldSha256"]:
                    raise UpdaterError(f"恢复后的 Skill 校验失败: {name}")
        elif destination.exists():
            try:
                remove_tree_checked(destination, f"fresh 新安装 Skill {name}")
            except UpdaterError as exc:
                raise UpdaterError(f"无法回滚新安装的 Skill: {destination}") from exc

    restored_skills = state.get("skills")
    if not isinstance(restored_skills, dict):
        restored_skills = {}
        state["skills"] = restored_skills
    for item in targets:
        if item["hadState"]:
            restored_skills[item["name"]] = json_clone(item["oldState"])
        else:
            restored_skills.pop(item["name"], None)
    durable_json_write(state_path(home), state)
    cleanup_transaction(rollback_root, journal_path, targets)
    return "rolled_back"


def scan_incomplete_transactions(home: Path) -> list[dict[str, Any]]:
    """Read and validate transaction metadata without changing disk state."""
    root = transaction_root(home)
    if root.is_symlink():
        raise UpdaterError(f"事务目录不允许符号链接: {root}")
    if not root.exists():
        return []
    if not root.is_dir():
        raise UpdaterError(f"事务路径不是目录: {root}")

    transactions: list[dict[str, Any]] = []
    for rollback_root in sorted(root.iterdir()):
        if (
            rollback_root.is_symlink()
            or not rollback_root.is_dir()
            or not re.fullmatch(r"[0-9a-f]{32}", rollback_root.name)
        ):
            raise UpdaterError(f"事务目录包含未知内容: {rollback_root}")
        journal_path = rollback_root / TRANSACTION_JOURNAL_NAME
        if not journal_path.exists():
            children = list(rollback_root.iterdir())
            temporary_journals: list[Path] = []
            for child in children:
                if (
                    child.is_symlink()
                    or not child.is_file()
                    or not re.fullmatch(
                        rf"\.{re.escape(TRANSACTION_JOURNAL_NAME)}\.[0-9a-f]{{32}}\.tmp",
                        child.name,
                    )
                ):
                    raise UpdaterError(
                        f"事务目录缺少 journal 且包含未知内容: {rollback_root}"
                    )
                temporary_journals.append(child)
            transactions.append(
                {
                    "rollbackRoot": rollback_root,
                    "targets": [],
                    "temporaryJournals": temporary_journals,
                }
            )
            continue
        if journal_path.is_symlink() or not journal_path.is_file():
            raise UpdaterError(f"事务 journal 不是安全文件: {journal_path}")
        payload = load_json(journal_path, {})
        targets = validate_transaction_journal(home, rollback_root, payload)
        transactions.append({"rollbackRoot": rollback_root, "targets": targets})
    return transactions


def recover_incomplete_transactions(home: Path) -> list[str]:
    recovered: list[str] = []
    for transaction in scan_incomplete_transactions(home):
        rollback_root = transaction["rollbackRoot"]
        if not transaction["targets"]:
            for temporary in transaction.get("temporaryJournals", []):
                try:
                    temporary.unlink()
                except OSError as exc:
                    raise UpdaterError(
                        f"无法清理未发布的事务临时 journal: {temporary}"
                    ) from exc
            fsync_directory(rollback_root)
            try:
                rollback_root.rmdir()
            except OSError as exc:
                raise UpdaterError(f"无法清理空事务目录: {rollback_root}") from exc
            recovered.append(rollback_root.name)
            continue
        recover_transaction_directory(home, rollback_root)
        recovered.append(rollback_root.name)
    return recovered


class SkillTransaction:
    def __init__(
        self,
        home: Path,
        targets: list[dict[str, Any]],
        staging: Path,
        state: Optional[dict[str, Any]] = None,
    ):
        self.home = home
        self.targets = targets
        self.staging = staging
        self.state = json_clone(state if state is not None else load_state(home))
        self.rollback_root = transaction_root(home) / uuid.uuid4().hex
        self.journal_path = self.rollback_root / TRANSACTION_JOURNAL_NAME
        self.moved: list[tuple[Path, Optional[Path]]] = []
        self.active = False

    def begin(self) -> list[dict[str, Any]]:
        skills_state = (
            self.state.get("skills") if isinstance(self.state.get("skills"), dict) else {}
        )
        journal_targets: list[dict[str, Any]] = []
        seen_names: set[str] = set()
        for item in self.targets:
            name = str(item.get("name", ""))
            destination = validate_transaction_destination(
                self.home, name, str(item.get("path", ""))
            )
            if name in seen_names:
                raise UpdaterError(f"事务 target 重复: {name}")
            seen_names.add(name)
            source = self.staging / "skills" / name
            if source.is_symlink() or not source.is_dir():
                raise UpdaterError(f"事务 staging Skill 无效: {name}")
            desired_hash = str(item.get("desiredSha256", ""))
            actual_desired_hash = directory_hash(source)
            if desired_hash and desired_hash != actual_desired_hash:
                raise UpdaterError(f"事务 staging Skill 哈希不匹配: {name}")
            if not desired_hash:
                desired_hash = actual_desired_hash

            existed = destination.exists()
            old_hash = checked_directory_hash(destination, f"事务原 Skill {name}")
            expected_old_hash = str(item.get("currentSha256", ""))
            if expected_old_hash and old_hash != expected_old_hash:
                raise UpdaterError(f"事务原 Skill 哈希已变化: {name}")
            had_state = name in skills_state
            journal_targets.append(
                {
                    "name": name,
                    "path": str(destination),
                    "existed": existed,
                    "hadState": had_state,
                    "oldState": json_clone(skills_state.get(name)) if had_state else None,
                    "oldSha256": old_hash or "",
                    "desiredSha256": desired_hash,
                }
            )

        parent = transaction_root(self.home)
        parent.mkdir(parents=True, exist_ok=True)
        self.rollback_root.mkdir()
        fsync_directory(parent)
        payload = {
            "schemaVersion": TRANSACTION_SCHEMA_VERSION,
            "kind": "operation-skill-update",
            "transactionId": self.rollback_root.name,
            "createdAt": utc_now(),
            "targets": journal_targets,
        }
        durable_json_write(self.journal_path, payload)
        self.active = True
        return validate_transaction_journal(self.home, self.rollback_root, payload)

    def apply(self) -> None:
        journal_targets = self.begin()
        try:
            for item in journal_targets:
                name = item["name"]
                destination = item["path"]
                rollback = item["rollback"]
                if item["existed"]:
                    os.replace(destination, rollback)
                    self.moved.append((destination, rollback))
                else:
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    self.moved.append((destination, None))
                source = self.staging / "skills" / name
                os.replace(source, destination)
        except BaseException:
            self.rollback()
            raise

    def rollback(self) -> None:
        if self.rollback_root.exists():
            recover_transaction_directory(
                self.home, self.rollback_root, force_rollback=True
            )
        self.moved.clear()
        self.active = False

    def commit(self) -> None:
        if self.rollback_root.exists():
            payload = load_json(self.journal_path, {})
            targets = validate_transaction_journal(
                self.home, self.rollback_root, payload
            )
            cleanup_transaction(self.rollback_root, self.journal_path, targets)
        self.moved.clear()
        self.active = False


def rollback_or_retain_pause(
    transaction: SkillTransaction, pause: UpdatePause
) -> None:
    """Rollback, keeping ESTOP engaged if recovery itself cannot finish."""
    try:
        transaction.rollback()
    except BaseException:
        pause.retain_for_recovery()
        raise


def refresh_bridge(home: Path, targets: list[dict[str, Any]]) -> None:
    ingest = next((item for item in targets if item["name"] == "facebook-video-ingest"), None)
    if not ingest:
        return
    installer = Path(ingest["path"]) / "scripts" / "install_hermes_worker.py"
    if not installer.is_file():
        raise UpdaterError(f"桥接安装器不存在: {installer}")
    environment = os.environ.copy()
    environment["HERMES_HOME"] = str(home)
    completed = subprocess.run(
        [sys.executable, str(installer), "--no-pairing-code"],
        env=environment,
        text=True,
        capture_output=True,
        check=False,
        timeout=300,
    )
    if completed.returncode:
        preview = (completed.stderr or completed.stdout or "unknown error")[-1000:]
        raise UpdaterError(f"Hermes Worker/Gateway 桥接刷新失败: {preview}")


def pending_bridge_repair(home: Path, state: dict[str, Any]) -> Optional[dict[str, Any]]:
    repair = state.get("bridgeRepair")
    if isinstance(repair, dict) and repair.get("status") == "pending":
        return dict(repair)
    sentinel = home / "ESTOP"
    try:
        payload = json.loads(sentinel.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or payload.get("reason") != "operation-skill-bridge-repair":
        return None
    commit = str(payload.get("commit", ""))
    sha256 = str(payload.get("sha256", ""))
    owner = str(payload.get("owner", ""))
    if (
        not re.fullmatch(r"[0-9a-f]{40}", commit)
        or not re.fullmatch(r"[0-9a-f]{64}", sha256)
        or not re.fullmatch(r"[0-9a-f]{32}", owner)
    ):
        return None
    repair = {
        "status": "pending",
        "commit": commit,
        "sha256": sha256,
        "path": str((home / "skills" / "facebook-video-ingest").resolve()),
        "sentinelOwner": owner,
        "error": "从 updater-owned ESTOP 恢复待修复状态",
        "changedSkills": payload.get("changedSkills", ["facebook-video-ingest"]),
        "reloadRequired": bool(payload.get("reloadRequired", True)),
        "updatedAt": utc_now(),
    }
    state["bridgeRepair"] = repair
    return dict(repair)


def bridge_repair_target(
    home: Path, repair: dict[str, Any]
) -> tuple[Optional[dict[str, Any]], str]:
    expected = str(repair.get("sha256", ""))
    commit = str(repair.get("commit", ""))
    if not re.fullmatch(r"[0-9a-f]{64}", expected):
        return None, "bridge repair state SHA-256 无效"
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        return None, "bridge repair state commit 无效"
    configured_path = str(repair.get("path", "")).strip()
    if not configured_path:
        return None, "bridge repair state 缺少 facebook-video-ingest 路径"
    path = Path(configured_path).expanduser().resolve()
    skills_root = (home / "skills").resolve()
    try:
        path.relative_to(skills_root)
    except ValueError:
        return None, "bridge repair state 路径不在 Hermes skills 目录内"
    if not path.is_dir():
        return None, "facebook-video-ingest 已缺失，无法安全修复 bridge"
    skill_md = path / "SKILL.md"
    try:
        if not skill_md.is_file() or skill_name(skill_md) != "facebook-video-ingest":
            return None, "bridge repair 路径不是 facebook-video-ingest"
    except (OSError, UpdaterError) as exc:
        return None, f"无法验证 facebook-video-ingest 身份: {exc}"
    try:
        actual = directory_hash(path)
    except (OSError, UpdaterError) as exc:
        return None, f"无法校验 facebook-video-ingest: {exc}"
    if actual != expected:
        return None, "facebook-video-ingest 已发生本地修改，拒绝运行未知 bridge 安装器"
    installer = path / "scripts" / "install_hermes_worker.py"
    if not installer.is_file():
        return None, "facebook-video-ingest bridge 安装器缺失"
    return {"name": "facebook-video-ingest", "path": str(path)}, ""


def make_bridge_repair(
    manifest: dict[str, Any],
    item: dict[str, Any],
    changed: list[str],
    previous: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    entry = next(entry for entry in manifest["skills"] if entry["name"] == item["name"])
    owner = str((previous or {}).get("sentinelOwner", ""))
    previous_changed = (previous or {}).get("changedSkills", [])
    carried = previous_changed if isinstance(previous_changed, list) else []
    changed_skills = list(dict.fromkeys([*carried, *changed]))
    return {
        "status": "pending",
        "commit": manifest["commit"],
        "releaseSequence": manifest["releaseSequence"],
        "sha256": entry["sha256"],
        "path": item["path"],
        "sentinelOwner": owner,
        "error": "bridge 刷新尚未完成",
        "changedSkills": changed_skills,
        "reloadRequired": bool(changed_skills),
        "updatedAt": utc_now(),
    }


def merge_bridge_repair_changes(
    repair: dict[str, Any], changed: list[str]
) -> dict[str, Any]:
    merged = dict(repair)
    carried = merged.get("changedSkills", [])
    if not isinstance(carried, list):
        carried = ["facebook-video-ingest"]
    changed_skills = list(dict.fromkeys([*carried, *changed]))
    merged["changedSkills"] = changed_skills
    merged["reloadRequired"] = bool(changed_skills) or bool(merged.get("reloadRequired"))
    return merged


def record_repaired_bridge_skill(
    state: dict[str, Any], repair: dict[str, Any], target: dict[str, Any]
) -> None:
    recorded = state.get("skills") if isinstance(state.get("skills"), dict) else {}
    state["skills"] = recorded
    previous = recorded.get("facebook-video-ingest")
    version = previous.get("version", "") if isinstance(previous, dict) else ""
    recorded["facebook-video-ingest"] = {
        "sha256": repair["sha256"],
        "version": version,
        "path": target["path"],
        "commit": repair["commit"],
        "recordedAt": utc_now(),
    }


def notify(title: str, message: str, disabled: bool) -> None:
    if disabled:
        return
    try:
        if sys.platform == "darwin":
            script = (
                "on run argv\n"
                "display notification (item 2 of argv) with title (item 1 of argv)\n"
                "end run"
            )
            subprocess.run(
                ["osascript", "-e", script, "--", title, message],
                check=False,
                timeout=10,
            )
        elif os.name == "nt":
            escaped_title = title.replace("'", "''")
            escaped_message = message.replace("'", "''")
            script = (
                "$shell=New-Object -ComObject WScript.Shell; "
                f"$null=$shell.Popup('{escaped_message}',10,'{escaped_title}',64)"
            )
            subprocess.Popen(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
    except (OSError, subprocess.SubprocessError):
        pass


def update_self(manifest: dict[str, Any], check_only: bool) -> bool:
    entry = manifest.get("updater")
    if not isinstance(entry, dict):
        return False
    current = Path(__file__).resolve()
    if sha256_file(current) == entry["sha256"]:
        return False
    if check_only:
        return True
    payload = request_bytes(
        entry["url"],
        limit=2 * 1024 * 1024,
        expected_size=entry["size"],
    )
    if sha256_bytes(payload) != entry["sha256"]:
        raise UpdaterError("更新器自身 SHA-256 校验失败")
    try:
        compile(payload.decode("utf-8"), str(current), "exec")
    except (UnicodeDecodeError, SyntaxError) as exc:
        raise UpdaterError(f"新更新器语法校验失败: {exc}") from exc
    temporary = current.with_name(f".{current.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        if os.name != "nt":
            temporary.chmod(0o755)
        durable_replace(temporary, current)
    finally:
        with contextlib.suppress(OSError):
            temporary.unlink()
    return True


def durable_file_write(path: Path, payload: bytes, mode: int = 0o700) -> None:
    """Atomically replace a file after flushing its contents and directory entry."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
        descriptor = os.open(temporary, flags, mode)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        if os.name != "nt":
            temporary.chmod(mode)
        durable_replace(temporary, path)
    finally:
        with contextlib.suppress(OSError):
            temporary.unlink()


def bootstrap_install(
    home: Path,
    manifest_file: Path,
    manifest_url: str,
    manage_core: bool,
    *,
    candidate_path: Optional[Path] = None,
) -> dict[str, Any]:
    """Install this verified updater while durably advancing anti-rollback state.

    Both platform installers execute the downloaded updater with this command.
    The state read, accepted-release write, executable replacement, and config
    write all happen while holding the same lock used by scheduled updates.
    """
    validate_url(manifest_url)
    try:
        manifest_bytes = manifest_file.read_bytes()
    except OSError as exc:
        raise UpdaterError(f"无法读取本地 manifest: {exc}") from exc
    if len(manifest_bytes) > 1024 * 1024:
        raise UpdaterError("本地 manifest 超过大小限制")
    try:
        manifest = validate_manifest(json.loads(manifest_bytes.decode("utf-8")))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise UpdaterError(f"本地 manifest JSON 无效: {exc}") from exc

    entry = manifest.get("updater")
    if not isinstance(entry, dict):
        raise UpdaterError("manifest updater 无效")
    source = (candidate_path or Path(__file__)).resolve()
    try:
        candidate = source.read_bytes()
    except OSError as exc:
        raise UpdaterError(f"无法读取待安装更新器: {exc}") from exc
    if len(candidate) != entry["size"] or sha256_bytes(candidate) != entry["sha256"]:
        raise UpdaterError("待安装更新器与 manifest 不匹配")
    try:
        compile(candidate.decode("utf-8"), str(source), "exec")
    except (UnicodeDecodeError, SyntaxError) as exc:
        raise UpdaterError(f"待安装更新器语法校验失败: {exc}") from exc

    destination = updater_home(home) / "operation_skill_updater.py"
    with updater_lock(home):
        # Re-read both files only after taking the lock.  An earlier preflight
        # read would allow a scheduled updater to commit stale state in between.
        state = load_state(home)
        validate_release_progress(manifest, state)
        config = load_json(config_path(home), {})
        if manage_core:
            managed = validate_skill_names(
                config.get("managedSkills"), "config managedSkills"
            )
            managed.update(CORE_SKILLS)
            config["managedSkills"] = sorted(managed)
        config["manifestUrl"] = manifest_url

        # This is intentionally the first committed mutation.  A crash before
        # or after executable replacement can leave installation incomplete,
        # but can no longer make an older release acceptable again.
        record_accepted_release(state, manifest)
        durable_json_write(state_path(home), state)
        durable_file_write(destination, candidate)
        durable_json_write(config_path(home), config)

    return {
        "status": "installed",
        "commit": manifest["commit"],
        "releaseSequence": manifest["releaseSequence"],
        "updater": str(destination),
    }


def result_payload(
    manifest: dict[str, Any], statuses: list[dict[str, Any]], **extra: Any
) -> dict[str, Any]:
    payload = {
        "checkedAt": utc_now(),
        "commit": manifest["commit"],
        "releaseSequence": manifest["releaseSequence"],
        "skills": statuses,
    }
    payload.update(extra)
    return payload


def run_check(home: Path, manifest_url: str) -> dict[str, Any]:
    manifest = fetch_manifest(manifest_url)
    state = load_state(home)
    validate_release_progress(manifest, state)
    remote_names = {entry["name"] for entry in manifest["skills"]}
    installed = find_installed_skills(home / "skills", remote_names)
    statuses = compute_statuses(manifest, installed, state)
    return result_payload(
        manifest,
        statuses,
        status="check",
        updaterUpdateAvailable=update_self(manifest, check_only=True),
    )


def run_update(
    home: Path,
    manifest_url: str,
    idle_timeout: int,
    no_notify: bool,
    install_names: Optional[set[str]] = None,
    adoption_names: Optional[set[str]] = None,
    adoption_release_sequence: Optional[int] = None,
) -> dict[str, Any]:
    with updater_lock(home):
        incomplete = scan_incomplete_transactions(home)
        recovery_state = load_state(home)
        claimed_owner = owned_update_sentinel_owner(home)
        repair_state = recovery_state.get("bridgeRepair")
        expected_repair_owner = (
            str(repair_state.get("sentinelOwner", ""))
            if isinstance(repair_state, dict) and repair_state.get("status") == "pending"
            else ""
        )
        orphan_sentinel_owner = (
            claimed_owner if claimed_owner != expected_repair_owner else ""
        )
        if incomplete or orphan_sentinel_owner:
            recovery_paths = list(
                dict.fromkeys(
                    item["path"]
                    for transaction in incomplete
                    for item in transaction["targets"]
                )
            )
            with paused_for_update(home, recovery_paths, idle_timeout) as recovery_pause:
                if not recovery_pause:
                    result = {
                        "checkedAt": utc_now(),
                        "status": "deferred",
                        "reason": "transaction-recovery-operation-skills-active",
                        "changedSkills": [],
                        "reloadRequired": False,
                    }
                    recovery_state["lastResult"] = result
                    durable_json_write(state_path(home), recovery_state)
                    notify(
                        "运营 Skill 崩溃恢复延期",
                        "检测到运营任务仍在运行，将在下次计划检查时继续恢复。",
                        no_notify,
                    )
                    return result
                if incomplete:
                    recover_incomplete_transactions(home)
                if orphan_sentinel_owner:
                    remove_owned_update_sentinel(home, orphan_sentinel_owner)

        manifest = fetch_manifest(manifest_url)
        remote_entries = {entry["name"]: entry for entry in manifest["skills"]}
        installed = find_installed_skills(home / "skills", set(remote_entries))
        state = load_state(home)
        validate_release_progress(manifest, state)

        # The accepted release is the durable anti-rollback lower bound.  It
        # must reach disk before the updater executable is replaced or before
        # any later early/deferred return.
        record_accepted_release(state, manifest)
        durable_json_write(state_path(home), state)

        explicit_installs = set(install_names or set())
        requested_adoptions = set(adoption_names or set())
        explicit_names = explicit_installs | requested_adoptions
        unknown = explicit_names - set(remote_entries)
        if unknown:
            raise UpdaterError("发布包不存在指定 Skill: " + ", ".join(sorted(unknown)))
        if requested_adoptions:
            if adoption_release_sequence is None:
                raise UpdaterError("采用现有 Skill 必须指定 --adopt-release-sequence")
            if adoption_release_sequence != manifest["releaseSequence"]:
                raise UpdaterError(
                    "采用确认的发布序列与当前发布不一致: "
                    f"confirmed={adoption_release_sequence}, "
                    f"current={manifest['releaseSequence']}"
                )
        elif adoption_release_sequence is not None:
            raise UpdaterError("--adopt-release-sequence 只能与采用现有 Skill 参数一起使用")

        managed = load_managed_skills(home)
        if explicit_names:
            managed = persist_managed_skills(home, explicit_names)
        requested = (managed & set(remote_entries)) | explicit_names
        unavailable_managed = sorted(managed - set(remote_entries))

        updater_changed = update_self(manifest, check_only=False)
        statuses = compute_statuses(manifest, installed, state)

        adoption_replacements: set[str] = set()
        aligned_adoptions: set[str] = set()
        for item in statuses:
            name = item["name"]
            if name not in requested_adoptions:
                continue
            if item["status"] == "unmanaged_existing":
                item["status"] = "update_available"
                item["adoption"] = True
                adoption_replacements.add(name)
            elif item["status"] == "up_to_date" and not item.get("hadBaseline"):
                item["adoption"] = True
                aligned_adoptions.add(name)

        for name in sorted(requested - set(installed)):
            entry = remote_entries[name]
            statuses.append(
                {
                    "name": name,
                    "path": str((home / "skills" / name).resolve()),
                    "status": "update_available",
                    "currentSha256": "",
                    "desiredSha256": entry["sha256"],
                    "version": entry.get("version", ""),
                    "hadBaseline": False,
                }
            )
        conflicts = [item for item in statuses if item["status"] == "local_modified"]
        unmanaged = [item for item in statuses if item["status"] == "unmanaged_existing"]
        adoption_blocked = sorted(
            item["name"] for item in conflicts if item["name"] in requested_adoptions
        )
        targets = [item for item in statuses if item["status"] == "update_available"]

        pipeline_blockers = {
            item["name"] for item in [*conflicts, *unmanaged]
        } & PIPELINE_SKILLS
        if pipeline_blockers and any(item["name"] in PIPELINE_SKILLS for item in targets):
            for item in targets:
                if item["name"] in PIPELINE_SKILLS:
                    item["status"] = "blocked_by_pipeline_conflict"
            targets = [item for item in targets if item["name"] not in PIPELINE_SKILLS]

        bridge_pending = pending_bridge_repair(home, state)
        changed: list[str] = []
        applied_changed: list[str] = []
        completed_adoptions: set[str] = set(aligned_adoptions)
        backup = ""
        adoption_backup = ""
        bridge_repaired = False
        if targets or bridge_pending:
            with paused_for_update(home, list(installed.values()), idle_timeout) as pause:
                if not pause:
                    pending = bridge_pending is not None
                    result = result_payload(
                        manifest,
                        statuses,
                        status="bridge_repair_pending" if pending else "deferred",
                        reason="operation-skills-active",
                        changedSkills=[],
                        localConflicts=[item["name"] for item in conflicts],
                        unmanagedExisting=[item["name"] for item in unmanaged],
                        adoptionBlocked=adoption_blocked,
                        unavailableManaged=unavailable_managed,
                        requiresAdminAdoption=bool(unmanaged),
                        bridgeRepair=bridge_pending if pending else None,
                        updaterUpdated=updater_changed,
                        reloadRequired=False,
                    )
                    if pending:
                        state["bridgeRepair"] = bridge_pending
                    state["lastResult"] = result
                    durable_json_write(state_path(home), state)
                    notify(
                        "运营 Skill bridge 修复待重试" if pending else "运营 Skill 更新延期",
                        "检测到运营任务正在运行，将在下次计划检查时重试。",
                        no_notify,
                    )
                    return result

                transaction: Optional[SkillTransaction] = None
                if targets:
                    archive = manifest["archive"]
                    archive_bytes = request_bytes(
                        archive["url"], limit=MAX_ARCHIVE_BYTES, expected_size=archive["size"]
                    )
                    if sha256_bytes(archive_bytes) != archive["sha256"]:
                        raise UpdaterError("发布 ZIP 的 SHA-256 校验失败")
                    temporary_parent = updater_home(home) / "tmp"
                    temporary_parent.mkdir(parents=True, exist_ok=True)
                    with tempfile.TemporaryDirectory(
                        prefix="operation-skill-update-", dir=temporary_parent
                    ) as temporary:
                        temp_root = Path(temporary)
                        archive_path = temp_root / "release.zip"
                        archive_path.write_bytes(archive_bytes)
                        staging = temp_root / "staging"
                        staging.mkdir()
                        safe_extract(archive_path, staging)
                        validate_staging(staging, manifest)
                        prepare_runtime_dependencies(home, staging, targets)
                        backup_root = backup_skills(home, targets)
                        backup = str(backup_root)
                        validate_targets_unchanged(targets)
                        adoption_targets = [
                            item for item in targets if item["name"] in adoption_replacements
                        ]
                        if adoption_targets:
                            adoption_backup = str(
                                backup_adopted_skills(home, adoption_targets)
                            )
                        transaction = SkillTransaction(home, targets, staging, state)
                        try:
                            transaction.apply()
                        except BaseException:
                            rollback_or_retain_pause(transaction, pause)
                            raise
                        changed = [item["name"] for item in targets]
                        applied_changed = list(changed)
                        completed_adoptions.update(adoption_replacements & set(changed))

                        ingest_item = next(
                            (item for item in targets if item["name"] == "facebook-video-ingest"),
                            None,
                        )
                        repair = (
                            make_bridge_repair(manifest, ingest_item, changed, bridge_pending)
                            if ingest_item is not None
                            else merge_bridge_repair_changes(bridge_pending, changed)
                            if bridge_pending is not None
                            else None
                        )
                        record_skill_versions(state, manifest, statuses, changed)
                        record_accepted_release(state, manifest)
                        record_adoption(
                            state,
                            manifest,
                            sorted(completed_adoptions),
                            adoption_backup,
                        )
                        if repair is not None:
                            try:
                                owner = pause.retain_for_bridge_repair(repair)
                                if owner:
                                    repair["sentinelOwner"] = owner
                                state["bridgeRepair"] = repair
                                state["lastResult"] = result_payload(
                                    manifest,
                                    statuses,
                                    status="bridge_repair_pending",
                                    reason="bridge-refresh-starting",
                                    changedSkills=changed,
                                    localConflicts=[item["name"] for item in conflicts],
                                    unmanagedExisting=[item["name"] for item in unmanaged],
                                    adoptionBlocked=adoption_blocked,
                                    adoptedSkills=sorted(completed_adoptions),
                                    adoptionBackup=adoption_backup,
                                    unavailableManaged=unavailable_managed,
                                    requiresAdminAdoption=bool(unmanaged),
                                    bridgeRepair=repair,
                                    updaterUpdated=updater_changed,
                                    backup=backup,
                                    reloadRequired=False,
                                )
                                durable_json_write(state_path(home), state)
                            except BaseException:
                                rollback_or_retain_pause(transaction, pause)
                                pause.cancel_retention()
                                raise
                        else:
                            try:
                                durable_json_write(state_path(home), state)
                            except BaseException:
                                rollback_or_retain_pause(transaction, pause)
                                raise
                        transaction.commit()
                        transaction = None

                ingest_item = next(
                    (item for item in targets if item["name"] == "facebook-video-ingest"),
                    None,
                )
                repair = (
                    make_bridge_repair(manifest, ingest_item, changed, bridge_pending)
                    if ingest_item is not None
                    else merge_bridge_repair_changes(bridge_pending, changed)
                    if bridge_pending is not None
                    else None
                )
                if repair is not None:
                    existing_repair = state.get("bridgeRepair")
                    existing_owner = (
                        str(existing_repair.get("sentinelOwner", ""))
                        if isinstance(existing_repair, dict)
                        else ""
                    )
                    current_owner = owned_bridge_sentinel_owner(home)
                    already_prepared = bool(
                        isinstance(existing_repair, dict)
                        and existing_owner
                        and existing_owner == current_owner
                    )
                    if already_prepared or pause.retain_sentinel:
                        repair = dict(existing_repair)
                    else:
                        promoted_owner = pause.retain_for_bridge_repair(repair)
                        if promoted_owner:
                            repair["sentinelOwner"] = promoted_owner
                        elif existing_owner != current_owner:
                            repair["sentinelOwner"] = ""
                        state["bridgeRepair"] = repair
                        record_skill_versions(state, manifest, statuses, changed)
                        record_accepted_release(state, manifest)
                        record_adoption(
                            state,
                            manifest,
                            sorted(completed_adoptions),
                            adoption_backup,
                        )
                        state["lastResult"] = result_payload(
                            manifest,
                            statuses,
                            status="bridge_repair_pending",
                            reason="bridge-refresh-starting",
                            changedSkills=changed,
                            localConflicts=[item["name"] for item in conflicts],
                            unmanagedExisting=[item["name"] for item in unmanaged],
                            adoptionBlocked=adoption_blocked,
                            adoptedSkills=sorted(completed_adoptions),
                            adoptionBackup=adoption_backup,
                            unavailableManaged=unavailable_managed,
                            requiresAdminAdoption=bool(unmanaged),
                            bridgeRepair=repair,
                            updaterUpdated=updater_changed,
                            backup=backup,
                            reloadRequired=False,
                        )
                        durable_json_write(state_path(home), state)

                    # An exact-match adoption needs no archive transaction but
                    # still needs a durable baseline and audit trail before a
                    # possibly failing bridge refresh.
                    record_skill_versions(state, manifest, statuses, applied_changed)
                    record_adoption(
                        state,
                        manifest,
                        sorted(completed_adoptions),
                        adoption_backup,
                    )

                    repair_target, bridge_error = bridge_repair_target(home, repair)
                    if repair_target is not None:
                        try:
                            refresh_bridge(home, [repair_target])
                        except Exception as exc:
                            bridge_error = str(exc) or exc.__class__.__name__
                    if bridge_error:
                        repair["status"] = "pending"
                        repair["error"] = bridge_error
                        repair["updatedAt"] = utc_now()
                        state["bridgeRepair"] = repair
                        result = result_payload(
                            manifest,
                            statuses,
                            status="bridge_repair_pending",
                            reason=bridge_error,
                            changedSkills=changed,
                            localConflicts=[item["name"] for item in conflicts],
                            unmanagedExisting=[item["name"] for item in unmanaged],
                            adoptionBlocked=adoption_blocked,
                            adoptedSkills=sorted(completed_adoptions),
                            adoptionBackup=adoption_backup,
                            unavailableManaged=unavailable_managed,
                            requiresAdminAdoption=bool(unmanaged),
                            bridgeRepair=repair,
                            updaterUpdated=updater_changed,
                            backup=backup,
                            reloadRequired=False,
                        )
                        state["lastResult"] = result
                        durable_json_write(state_path(home), state)
                        notify(
                            "运营 Skill bridge 修复待重试",
                            "新版 Skill 已保留；bridge 刷新失败，将在下次计划检查时重试。",
                            no_notify,
                        )
                        return result

                    assert repair_target is not None
                    record_repaired_bridge_skill(state, repair, repair_target)
                    carried = repair.get("changedSkills", [])
                    if not isinstance(carried, list):
                        carried = ["facebook-video-ingest"]
                    changed = list(dict.fromkeys([*carried, *changed]))
                    pause.release_after_bridge_repair(str(repair.get("sentinelOwner", "")))
                    state.pop("bridgeRepair", None)
                    bridge_repaired = True

        record_skill_versions(state, manifest, statuses, applied_changed)
        record_accepted_release(state, manifest)
        record_adoption(
            state,
            manifest,
            sorted(completed_adoptions),
            adoption_backup,
        )
        final_status = "updated" if changed or updater_changed else "up_to_date"
        if conflicts and not changed:
            final_status = "local_conflict"
        elif unmanaged and not changed:
            final_status = "adoption_required"
        result = result_payload(
            manifest,
            statuses,
            status=final_status,
            changedSkills=changed,
            localConflicts=[item["name"] for item in conflicts],
            unmanagedExisting=[item["name"] for item in unmanaged],
            adoptionBlocked=adoption_blocked,
            adoptedSkills=sorted(completed_adoptions),
            adoptionBackup=adoption_backup,
            unavailableManaged=unavailable_managed,
            requiresAdminAdoption=bool(unmanaged),
            updaterUpdated=updater_changed,
            bridgeRepaired=bridge_repaired,
            backup=backup,
            reloadRequired=bool(changed),
        )
        state["lastResult"] = result
        durable_json_write(state_path(home), state)
        if bridge_repaired:
            notify(
                "运营 Skill bridge 已修复",
                f"已完成 bridge 修复；{len(changed)} 个已更新 Skill 现在可以重新加载。",
                no_notify,
            )
        elif changed:
            notify("运营 Skill 已更新", f"已更新 {len(changed)} 个 Skill；已打开的 Hermes 请执行 /reload-skills。", no_notify)
        elif conflicts:
            notify("运营 Skill 更新需处理", "检测到本地修改，已保留文件并跳过覆盖。", no_notify)
        elif unmanaged:
            notify(
                "运营 Skill 需要管理员采用",
                "检测到旧的人工安装；已保留文件，管理员确认后可执行一次采用安装。",
                no_notify,
            )
        return result


def schedule_minute() -> int:
    identity = os.getenv("COMPUTERNAME") or os.getenv("HOSTNAME") or str(Path.home())
    return int(hashlib.sha256(identity.encode("utf-8")).hexdigest(), 16) % 30


def powershell_encoded(script: str) -> str:
    return base64.b64encode(script.encode("utf-16le")).decode("ascii")


def windows_task_name(home: Path) -> str:
    identity = ntpath.normcase(ntpath.normpath(str(home)))
    suffix = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:12]
    return f"HM Operation Skill Updater-{suffix}"


def windows_schedule_context(home: Path) -> str:
    script_path = Path(__file__).resolve()
    argument = f'"{script_path}" --hermes-home "{home}" run'
    return f"""
$currentUser = [Security.Principal.WindowsIdentity]::GetCurrent()
$userId = $currentUser.User.Value
$action = New-ScheduledTaskAction -Execute '{str(Path(sys.executable)).replace("'", "''")}' -Argument '{argument.replace("'", "''")}'
""".strip()


def windows_legacy_schedule_cleanup() -> str:
    # Migrate only the old task belonging to this user AND installation.
    return """
$legacy = Get-ScheduledTask -TaskName 'HM Operation Skill Updater' -TaskPath '\\' -ErrorAction SilentlyContinue
if ($legacy) {
  $legacyUser = $legacy.Principal.UserId
  $ours = ($legacyUser -eq $userId -or $legacyUser -eq $currentUser.Name)
  if ($ours -and @($legacy.Actions).Count -eq 1 -and
      $legacy.Actions[0].Execute -eq $action.Execute -and
      $legacy.Actions[0].Arguments -eq $action.Arguments) {
    try {
      $legacy | Unregister-ScheduledTask -Confirm:$false -ErrorAction Stop
    } catch {
      [Console]::Out.WriteLine('WARNING: The matching legacy task could not be removed; ask IT to inspect HM Operation Skill Updater.')
    }
  }
}
""".strip()


def windows_schedule_script(home: Path, minute: int) -> str:
    task_name = windows_task_name(home)
    return f"""
{windows_schedule_context(home)}
$triggers = @(
  (New-ScheduledTaskTrigger -AtLogOn -User $userId)
  (New-ScheduledTaskTrigger -Daily -At '04:{minute:02d}')
)
$principal = New-ScheduledTaskPrincipal -UserId $userId -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Hours 2)
Register-ScheduledTask -TaskName '{task_name}' -TaskPath '\\' -Action $action -Trigger $triggers -Principal $principal -Settings $settings -Force -ErrorAction Stop | Out-Null
{windows_legacy_schedule_cleanup()}
""".strip()


def run_windows_schedule_script(script: str) -> None:
    wrapped = """
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
try {
""" + script + """
  # Optional lookups (for an absent legacy task) may leave $? false even
  # though registration succeeded. Do not leak that as process exit code 1.
  exit 0
} catch {
  [Console]::Error.WriteLine($_.Exception.Message)
  [Console]::Error.WriteLine($_.FullyQualifiedErrorId)
  exit 1
}
"""
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand", powershell_encoded(wrapped)],
            check=False,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
        )
    except subprocess.TimeoutExpired as exc:
        raise UpdaterError("Windows 自动更新计划操作超时；请联系网管检查任务计划程序服务。") from exc
    except OSError as exc:
        raise UpdaterError("无法启动 Windows PowerShell；请联系网管检查安装环境。") from exc
    if result.returncode:
        detail = (result.stderr or result.stdout or f"exit {result.returncode}").strip()
        if "80070005" in detail or "access is denied" in detail.lower() or "拒绝访问" in detail:
            raise UpdaterError(
                "Windows 拒绝创建或修改当前用户的自动更新计划 (0x80070005)。"
                "Skill 文件不会因此删除。请让网管检查当前账号的计划任务权限；"
                "不要换成其他账号重装，也不要关闭安全策略。"
            )
        raise UpdaterError(f"Windows 自动更新计划操作失败：{detail[:800]}")
    if result.stdout.strip():
        print(result.stdout.strip(), file=sys.stderr)


def install_schedule(home: Path, dry_run: bool) -> dict[str, Any]:
    minute = schedule_minute()
    script_path = Path(__file__).resolve()
    log_dir = updater_home(home) / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        task_name = windows_task_name(home)
        ps = windows_schedule_script(home, minute)
        if not dry_run:
            run_windows_schedule_script(ps)
        return {"platform": "windows", "task": task_name, "dailyAt": f"04:{minute:02d}", "dryRun": dry_run}
    if sys.platform == "darwin":
        label = "com.hm.operation-skill-updater"
        plist_path = Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"
        payload = {
            "Label": label,
            "ProgramArguments": [sys.executable, str(script_path), "--hermes-home", str(home), "run"],
            "RunAtLoad": True,
            "StartCalendarInterval": {"Hour": 4, "Minute": minute},
            "StandardOutPath": str(log_dir / "launchd.out.log"),
            "StandardErrorPath": str(log_dir / "launchd.err.log"),
            "WorkingDirectory": str(updater_home(home)),
        }
        if not dry_run:
            plist_path.parent.mkdir(parents=True, exist_ok=True)
            plist_path.write_bytes(plistlib.dumps(payload, sort_keys=True))
            domain = f"gui/{os.getuid()}"
            subprocess.run(["launchctl", "bootout", domain, str(plist_path)], check=False, capture_output=True)
            subprocess.run(["launchctl", "bootstrap", domain, str(plist_path)], check=True)
        return {"platform": "macos", "plist": str(plist_path), "dailyAt": f"04:{minute:02d}", "dryRun": dry_run}
    raise UpdaterError("自动计划任务目前只支持 Windows 和 macOS")


def uninstall_schedule(home: Path, dry_run: bool) -> dict[str, Any]:
    if os.name == "nt":
        task_name = windows_task_name(home)
        ps = f"""
{windows_schedule_context(home)}
$task = Get-ScheduledTask -TaskName '{task_name}' -TaskPath '\\' -ErrorAction SilentlyContinue
if ($task) {{
  $task | Unregister-ScheduledTask -Confirm:$false -ErrorAction Stop
}}
{windows_legacy_schedule_cleanup()}
"""
        if not dry_run:
            run_windows_schedule_script(ps)
        return {"platform": "windows", "removed": task_name, "dryRun": dry_run}
    if sys.platform == "darwin":
        label = "com.hm.operation-skill-updater"
        plist_path = Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"
        if not dry_run:
            domain = f"gui/{os.getuid()}"
            subprocess.run(["launchctl", "bootout", domain, str(plist_path)], check=False, capture_output=True)
            with contextlib.suppress(OSError):
                plist_path.unlink()
        return {"platform": "macos", "removed": str(plist_path), "dryRun": dry_run}
    raise UpdaterError("自动计划任务目前只支持 Windows 和 macOS")


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hermes-home", type=Path, default=default_hermes_home())
    parser.add_argument("--manifest-url", default="")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--no-notify", action="store_true")
    parser.add_argument("--idle-timeout", type=int, default=120)
    subparsers = parser.add_subparsers(dest="command", required=True)
    run = subparsers.add_parser("run")
    run.add_argument("--install", action="append", default=[], metavar="SKILL")
    run.add_argument("--install-core", action="store_true")
    run.add_argument("--adopt-existing", action="append", default=[], metavar="SKILL")
    run.add_argument("--adopt-existing-core", action="store_true")
    run.add_argument("--adopt-release-sequence", type=int)
    subparsers.add_parser("check")
    subparsers.add_parser("status")
    install = subparsers.add_parser("install-schedule")
    install.add_argument("--dry-run", action="store_true")
    uninstall = subparsers.add_parser("uninstall-schedule")
    uninstall.add_argument("--dry-run", action="store_true")
    bootstrap = subparsers.add_parser("bootstrap-install")
    bootstrap.add_argument("--manifest-file", required=True, type=Path)
    bootstrap.add_argument("--manage-core", action="store_true")
    return parser.parse_args(argv)


def render(payload: dict[str, Any], as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return
    status = payload.get("status", "ok")
    print(f"状态: {status}")
    if payload.get("commit"):
        print(f"版本: {payload['commit']}")
    if payload.get("changedSkills"):
        print("已更新: " + ", ".join(payload["changedSkills"]))
    if payload.get("localConflicts"):
        print("保留本地修改: " + ", ".join(payload["localConflicts"]))
    if payload.get("unmanagedExisting"):
        print("等待管理员采用: " + ", ".join(payload["unmanagedExisting"]))
    if payload.get("adoptedSkills"):
        print("已采用现有 Skill: " + ", ".join(payload["adoptedSkills"]))
    if payload.get("adoptionBackup"):
        print(f"永久采用备份: {payload['adoptionBackup']}")
    if payload.get("reloadRequired"):
        print("已打开的 Hermes 会话请执行 /reload-skills")
    if payload.get("task"):
        print(f"自动更新任务: {payload['task']}")
        print(f"检查时间: 当前用户登录后，以及每天 {payload['dailyAt']}（当前用户登录期间）")


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    home = args.hermes_home.expanduser().resolve()
    try:
        if args.command == "status":
            payload = load_state(home).get("lastResult") or {"status": "never-run"}
        elif args.command == "install-schedule":
            payload = install_schedule(home, args.dry_run)
        elif args.command == "uninstall-schedule":
            payload = uninstall_schedule(home, args.dry_run)
        elif args.command == "bootstrap-install":
            if not args.manifest_url:
                raise UpdaterError("bootstrap-install 必须指定 --manifest-url")
            payload = bootstrap_install(
                home,
                args.manifest_file.expanduser().resolve(),
                args.manifest_url,
                args.manage_core,
            )
        else:
            manifest_url = resolve_manifest_url(home, args.manifest_url)
            if args.command == "check":
                payload = run_check(home, manifest_url)
            else:
                install_names = set(args.install)
                if args.install_core:
                    install_names.update(CORE_SKILLS)
                adoption_names = set(args.adopt_existing)
                if args.adopt_existing_core:
                    adoption_names.update(CORE_SKILLS)
                if args.install_core or args.adopt_existing_core:
                    # Core names are built into this trusted updater, so the
                    # retry intent can safely survive even a manifest outage.
                    # The merge still uses updater.lock so it cannot restore an
                    # older manifestUrl over a concurrent bootstrap install.
                    persist_managed_skills_locked(home, set(CORE_SKILLS))
                payload = run_update(
                    home,
                    manifest_url,
                    max(0, args.idle_timeout),
                    args.no_notify,
                    install_names,
                    adoption_names,
                    args.adopt_release_sequence,
                )
        render(payload, args.json)
        return (
            1
            if payload.get("status") == "bridge_repair_pending"
            or payload.get("requiresAdminAdoption")
            or payload.get("adoptionBlocked")
            else 0
        )
    except Exception as exc:
        message = str(exc) or exc.__class__.__name__
        with contextlib.suppress(Exception):
            log_message(home, f"ERROR {message}")
        # Failure reporting must merge into the latest state while holding the
        # same lock as bootstrap/run.  Otherwise a stale error snapshot could
        # overwrite a concurrently advanced anti-rollback lower bound.
        if not isinstance(exc, UpdaterBusyError):
            with contextlib.suppress(Exception):
                with updater_lock(home):
                    state = load_state(home)
                    state["lastResult"] = {
                        "status": "failed",
                        "failedAt": utc_now(),
                        "error": message,
                    }
                    durable_json_write(state_path(home), state)
        notify("运营 Skill 更新失败", message[:180], args.no_notify)
        if args.json:
            print(json.dumps({"status": "failed", "error": message}, ensure_ascii=False))
        else:
            print(f"更新失败: {message}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
