#!/usr/bin/env python3
"""Upload a user-selected file or directory to one or more MYT cloud phones."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import http.client
import json
import mimetypes
import os
from pathlib import Path
import posixpath
import re
import shlex
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from typing import Any


VERSION = "2.0.0"
USER_AGENT = f"Hermes-MYT-File-Upload/{VERSION}"
# Real-device feedback confirms POST /upload stores files here.
UPLOAD_STAGING_DIR = "/sdcard/upload"
DEFAULT_REMOTE_DIR = "/sdcard/upload"
PRINT_LOCK = threading.Lock()
REMOTE_COMPONENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
REMOTE_DIR_RE = re.compile(r"^/[A-Za-z0-9._/-]+$")


class ConfigurationError(ValueError):
    """Raised when required configuration or arguments are invalid."""


class MytError(RuntimeError):
    """Raised when a MYT operation fails."""


@dataclass(frozen=True)
class Device:
    label: str
    port: int


@dataclass(frozen=True)
class InputFile:
    local_path: Path
    relative_remote_path: str
    size: int


def log(message: str, *, error: bool = False) -> None:
    with PRINT_LOCK:
        print(message, file=sys.stderr if error else sys.stdout, flush=True)


def normalize_host(value: str) -> str:
    host = value.strip().rstrip("/")
    if "://" in host:
        parsed = urllib.parse.urlsplit(host)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ConfigurationError("MYT host is not a valid hostname or IP address")
        if parsed.port:
            raise ConfigurationError("Do not include a port in --host; use --devices")
        host = parsed.hostname
    if not host or "/" in host or ":" in host:
        raise ConfigurationError(
            "MYT host must be a hostname or IPv4 address without protocol or port"
        )
    return host


def parse_devices(value: str, base_port: int, stride: int) -> list[Device]:
    if not value or not value.strip():
        raise ConfigurationError(
            "Target devices are required; pass --devices T1001,T1002"
        )
    devices: list[Device] = []
    seen_ports: set[int] = set()
    for raw_item in value.split(","):
        item = raw_item.strip()
        if not item:
            continue
        match = re.fullmatch(r"(?i)T100(\d+)", item)
        if match:
            index = int(match.group(1))
            if index < 1:
                raise ConfigurationError(f"Invalid MYT device ID: {item}")
            port = base_port + (index - 1) * stride
            label = f"T100{index}"
        elif item.isdigit():
            port = int(item)
            label = f"port-{port}"
        else:
            raise ConfigurationError(
                f"Invalid device '{item}'; use IDs such as T1001 or numeric ports"
            )
        if not 1 <= port <= 65535:
            raise ConfigurationError(f"Port outside valid range: {port}")
        if port not in seen_ports:
            devices.append(Device(label=label, port=port))
            seen_ports.add(port)
    if not devices:
        raise ConfigurationError("At least one MYT device ID or port is required")
    return devices


def validate_input_path(value: str) -> Path:
    if not value or not value.strip():
        raise ConfigurationError(
            "Local file or directory path is required; pass --path with a "
            "user-supplied path"
        )
    path = Path(value).expanduser().resolve()
    if not path.exists():
        raise ConfigurationError(f"Local path does not exist: {path}")
    if not path.is_file() and not path.is_dir():
        raise ConfigurationError(f"Local path is not a regular file or directory: {path}")
    return path


def portable_remote_component(value: str) -> str:
    if REMOTE_COMPONENT_RE.fullmatch(value) and value not in {".", ".."}:
        return value
    original = value
    suffix = Path(value).suffix
    ascii_suffix = suffix if re.fullmatch(r"\.[A-Za-z0-9]{1,16}", suffix) else ""
    stem_value = value[: -len(suffix)] if ascii_suffix else value
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", stem_value).strip("._-")
    if not stem:
        stem = "file"
    digest = hashlib.sha256(original.encode("utf-8")).hexdigest()[:8]
    return f"{stem[:100]}-{digest}{ascii_suffix}"


def validate_remote_name(value: str) -> str:
    name = value.strip()
    if Path(name).name != name or not REMOTE_COMPONENT_RE.fullmatch(name):
        raise ConfigurationError(
            "--remote-name must be an ASCII filename using letters, numbers, "
            "dots, underscores, or hyphens"
        )
    if name in {".", ".."}:
        raise ConfigurationError("Remote filename is invalid")
    return name


def collect_input_files(path: Path, requested_remote_name: str | None) -> list[InputFile]:
    if path.is_file():
        if path.is_symlink():
            raise ConfigurationError("Symbolic-link input files are not supported")
        name = (
            validate_remote_name(requested_remote_name)
            if requested_remote_name
            else portable_remote_component(path.name)
        )
        return [InputFile(path, name, path.stat().st_size)]

    if requested_remote_name:
        raise ConfigurationError("--remote-name can only be used with a single file")

    entries: list[InputFile] = []
    remote_paths: set[str] = set()
    for candidate in sorted(path.rglob("*"), key=lambda item: str(item).casefold()):
        if candidate.is_symlink() or not candidate.is_file():
            continue
        relative = candidate.relative_to(path)
        remote_relative = "/".join(
            portable_remote_component(part) for part in relative.parts
        )
        if remote_relative in remote_paths:
            raise ConfigurationError(
                f"Two local files map to the same remote path: {remote_relative}"
            )
        remote_paths.add(remote_relative)
        entries.append(
            InputFile(candidate, remote_relative, candidate.stat().st_size)
        )
    if not entries:
        raise ConfigurationError(f"Directory contains no regular files: {path}")
    return entries


def validate_remote_dir(value: str) -> str:
    remote_dir = value.strip().rstrip("/") or "/"
    if (
        not REMOTE_DIR_RE.fullmatch(remote_dir)
        or "/../" in f"{remote_dir}/"
        or "/./" in f"{remote_dir}/"
    ):
        raise ConfigurationError(
            "--remote-dir must be a safe absolute Android path without '..'"
        )
    return remote_dir


def human_size(value: int) -> str:
    size = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or unit == "TiB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{value} B"


class MytClient:
    def __init__(
        self,
        host: str,
        device: Device,
        *,
        request_timeout: float,
        verbose: bool,
    ) -> None:
        self.host = normalize_host(host)
        self.device = device
        self.request_timeout = request_timeout
        self.verbose = verbose

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.device.port}"

    def shell(self, command: str, timeout: float = 30.0) -> bytes:
        query = urllib.parse.urlencode({"cmd": "6", "cmdline": command})
        url = f"{self.base_url}/modifydev?{query}"
        if self.verbose:
            log(f"[{self.device.label}] shell: {command}")
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = response.read(1024 * 1024)
                if response.status < 200 or response.status >= 300:
                    raise MytError(f"shell returned HTTP {response.status}")
                self._raise_api_error(payload, "shell")
                return payload
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise MytError(
                f"{self.device.label} ({self.device.port}) shell request failed: {exc}"
            ) from exc

    @staticmethod
    def _raise_api_error(payload: bytes, operation: str) -> None:
        try:
            parsed = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return
        if isinstance(parsed, dict) and parsed.get("code") not in {None, 0, 200}:
            reason = parsed.get("reason") or parsed.get("error") or parsed.get("msg")
            raise MytError(f"{operation} failed: {reason or parsed}")

    def download_small(self, path: str) -> bytes:
        query = urllib.parse.urlencode({"path": path})
        url = f"{self.base_url}/download?{query}"
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                payload = response.read(1024 * 1024)
                if response.status < 200 or response.status >= 300:
                    raise MytError(f"download returned HTTP {response.status}")
                self._raise_api_error(payload, "download")
                return payload
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise MytError(
                f"{self.device.label} ({self.device.port}) download failed: {exc}"
            ) from exc

    def check(self) -> None:
        self.shell("true")

    def remote_size(self, path: str) -> int | None:
        quoted = shlex.quote(path)
        marker_path = f"/sdcard/.hermes-size-{uuid.uuid4().hex}.txt"
        marker_q = shlex.quote(marker_path)
        command = (
            f"if [ -f {quoted} ]; then "
            f"printf 'SIZE:' > {marker_q}; wc -c < {quoted} >> {marker_q}; "
            f"else printf 'MISSING' > {marker_q}; fi"
        )
        try:
            self.shell(command)
            payload = self.download_small(marker_path)
            text = payload.decode("utf-8", errors="replace").strip()
            if text == "MISSING":
                return None
            match = re.fullmatch(r"SIZE:\s*(\d+)", text)
            if not match:
                raise MytError(f"Could not read remote size for {path}")
            return int(match.group(1))
        finally:
            try:
                self.remove(marker_path)
            except Exception:
                pass

    def upload(
        self,
        local_path: Path,
        multipart_filename: str,
        *,
        progress_step: int,
    ) -> str:
        boundary = f"----HermesMYT{uuid.uuid4().hex}"
        content_type = mimetypes.guess_type(local_path.name)[0] or "application/octet-stream"
        prefix = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; '
            f'filename="{multipart_filename}"\r\n'
            f"Content-Type: {content_type}\r\n\r\n"
        ).encode("ascii")
        suffix = f"\r\n--{boundary}--\r\n".encode("ascii")
        file_size = local_path.stat().st_size
        total_length = len(prefix) + file_size + len(suffix)
        connection = http.client.HTTPConnection(
            self.host,
            self.device.port,
            timeout=self.request_timeout,
        )
        sent = 0
        next_report = max(1, progress_step)
        try:
            connection.putrequest("POST", "/upload")
            connection.putheader("User-Agent", USER_AGENT)
            connection.putheader(
                "Content-Type", f"multipart/form-data; boundary={boundary}"
            )
            connection.putheader("Content-Length", str(total_length))
            connection.endheaders()
            connection.send(prefix)
            with local_path.open("rb") as source:
                while True:
                    chunk = source.read(1024 * 1024)
                    if not chunk:
                        break
                    connection.send(chunk)
                    sent += len(chunk)
                    percent = int(sent * 100 / file_size)
                    if percent >= next_report or sent == file_size:
                        log(
                            f"[{self.device.label}] uploading "
                            f"{min(percent, 100)}% ({human_size(sent)}/{human_size(file_size)})"
                        )
                        while next_report <= percent:
                            next_report += max(1, progress_step)
            connection.send(suffix)
            response = connection.getresponse()
            payload = response.read(1024 * 1024)
            text = payload.decode("utf-8", errors="replace").strip()
            if response.status < 200 or response.status >= 300:
                raise MytError(
                    f"upload returned HTTP {response.status}: {text[:300]}"
                )
            self._raise_api_error(payload, "upload")
            return text
        except (OSError, TimeoutError, http.client.HTTPException) as exc:
            raise MytError(
                f"{self.device.label} upload failed after {human_size(sent)}: {exc}"
            ) from exc
        finally:
            connection.close()

    def finalize(
        self,
        temporary_path: str,
        final_path: str,
        *,
        overwrite: bool,
    ) -> None:
        temp_q = shlex.quote(temporary_path)
        final_q = shlex.quote(final_path)
        parent_q = shlex.quote(posixpath.dirname(final_path))
        overwrite_flag = " -f" if overwrite else ""
        command = (
            f"mkdir -p {parent_q} && "
            f"mv{overwrite_flag} {temp_q} {final_q}"
        )
        self.shell(command)

    def remove(self, path: str) -> None:
        self.shell(f"rm -f {shlex.quote(path)}")

    def scan_media(self, path: str) -> None:
        uri = f"file://{path}"
        self.shell(
            "am broadcast -a android.intent.action.MEDIA_SCANNER_SCAN_FILE "
            f"-d {shlex.quote(uri)}"
        )


def result_base(device: Device, final_path: str) -> dict[str, Any]:
    return {
        "device": device.label,
        "port": device.port,
        "remote_path": final_path,
    }


def run_check(client: MytClient) -> dict[str, Any]:
    result = result_base(client.device, "")
    try:
        client.check()
        result.update(status="ok")
    except Exception as exc:
        result.update(status="failed", error=str(exc))
    return result


def upload_file(
    client: MytClient,
    entry: InputFile,
    remote_dir: str,
    *,
    overwrite: bool,
    progress_step: int,
) -> dict[str, Any]:
    final_path = f"{remote_dir}/{entry.relative_remote_path}"
    result: dict[str, Any] = {
        "local_path": str(entry.local_path),
        "remote_path": final_path,
        "bytes": entry.size,
    }
    local_path = entry.local_path
    local_size = local_path.stat().st_size
    temp_name = f"hermes-{uuid.uuid4().hex}.upload"
    temporary_path = f"{UPLOAD_STAGING_DIR}/{temp_name}"
    try:
        existing_size = client.remote_size(final_path)
        result["existing_bytes"] = existing_size
        if existing_size == local_size:
            result.update(status="already-present")
            return result
        if existing_size is not None and not overwrite:
            result.update(
                status="conflict",
                error="remote file exists with a different size; use --overwrite explicitly",
            )
            return result
        log(
            f"[{client.device.label}] uploading {local_path.name} "
            f"({human_size(local_size)})"
        )
        response = client.upload(
            local_path,
            temp_name,
            progress_step=progress_step,
        )
        result["upload_response"] = response[:300]
        uploaded_size = client.remote_size(temporary_path)
        if uploaded_size != local_size:
            client.remove(temporary_path)
            raise MytError(
                f"temporary upload size mismatch: local={local_size}, "
                f"remote={uploaded_size}"
            )
        if existing_size is not None and overwrite:
            client.remove(final_path)
        client.finalize(
            temporary_path,
            final_path,
            overwrite=overwrite,
        )
        final_size = client.remote_size(final_path)
        if final_size != local_size:
            raise MytError(
                f"final file size mismatch: local={local_size}, remote={final_size}"
            )
        client.scan_media(final_path)
        result.update(status="uploaded", verified_bytes=final_size)
        return result
    except Exception as exc:
        try:
            client.remove(temporary_path)
        except Exception:
            pass
        result.update(status="failed", error=str(exc))
        return result


def upload_device(
    client: MytClient,
    entries: list[InputFile],
    remote_dir: str,
    *,
    execute: bool,
    overwrite: bool,
    progress_step: int,
) -> dict[str, Any]:
    result = result_base(client.device, remote_dir)
    result["total_files"] = len(entries)
    result["total_bytes"] = sum(entry.size for entry in entries)
    try:
        client.check()
        if not execute:
            result["files"] = [
                {
                    "local_path": str(entry.local_path),
                    "remote_path": f"{remote_dir}/{entry.relative_remote_path}",
                    "bytes": entry.size,
                    "status": "preview",
                }
                for entry in entries
            ]
            result.update(status="preview")
            return result

        file_results: list[dict[str, Any]] = []
        for index, entry in enumerate(entries, start=1):
            log(
                f"[{client.device.label}] file {index}/{len(entries)}: "
                f"{entry.relative_remote_path}"
            )
            file_results.append(
                upload_file(
                    client,
                    entry,
                    remote_dir,
                    overwrite=overwrite,
                    progress_step=progress_step,
                )
            )
        result["files"] = file_results
        result["uploaded"] = sum(
            item["status"] == "uploaded" for item in file_results
        )
        result["already_present"] = sum(
            item["status"] == "already-present" for item in file_results
        )
        result["conflicts"] = sum(
            item["status"] == "conflict" for item in file_results
        )
        result["failed"] = sum(item["status"] == "failed" for item in file_results)
        if result["failed"] or result["conflicts"]:
            result["status"] = "partial" if (
                result["uploaded"] or result["already_present"]
            ) else "failed"
        else:
            result["status"] = "ok"
        return result
    except Exception as exc:
        result.update(status="failed", files=[], error=str(exc))
        return result


def print_results(results: list[dict[str, Any]], *, json_output: bool) -> None:
    if json_output:
        print(json.dumps(results, ensure_ascii=False, indent=2))
        return
    log("")
    log("MYT file upload summary")
    for item in results:
        message = (
            f"  {item['device']} port={item['port']} "
            f"status={item['status']}"
        )
        if item.get("total_files") is not None:
            message += (
                f" files={item.get('uploaded', 0)} uploaded, "
                f"{item.get('already_present', 0)} already-present, "
                f"{item.get('conflicts', 0)} conflicts, "
                f"{item.get('failed', 0)} failed / {item['total_files']} total"
            )
        if item.get("error"):
            message += f" error={item['error']}"
        log(message, error=item["status"] in {"failed", "partial"})
        for file_item in item.get("files", []):
            detail = (
                f"    {file_item['status']}: {file_item['remote_path']} "
                f"({human_size(int(file_item['bytes']))})"
            )
            if file_item.get("error"):
                detail += f" error={file_item['error']}"
            log(
                detail,
                error=file_item["status"] in {"failed", "conflict"},
            )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Upload a user-selected file or directory to MYT cloud phones"
    )
    parser.add_argument(
        "--path",
        dest="input_path",
        help="Local file or directory path; required for every upload",
    )
    parser.add_argument(
        "--file",
        dest="legacy_file",
        help=argparse.SUPPRESS,
    )
    parser.add_argument("--devices", help="Comma-separated MYT IDs or numeric ports")
    parser.add_argument("--host", default=os.getenv("MYT_HOST", ""))
    parser.add_argument(
        "--base-port",
        type=int,
        default=int(os.getenv("MYT_BASE_PORT", "10005")),
    )
    parser.add_argument(
        "--port-stride",
        type=int,
        default=int(os.getenv("MYT_PORT_STRIDE", "3")),
    )
    parser.add_argument("--remote-dir", default=DEFAULT_REMOTE_DIR)
    parser.add_argument("--remote-name")
    parser.add_argument(
        "--workers",
        type=int,
        default=0,
        help="Concurrent devices; default is all selected devices (max 8)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=3600,
        help="Per-device upload network timeout in seconds (default: 3600)",
    )
    parser.add_argument(
        "--progress-step",
        type=int,
        default=10,
        help="Print progress every N percent (default: 10)",
    )
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--version", action="version", version=VERSION)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if not args.host:
            raise ConfigurationError("MYT_HOST is missing; set it or pass --host")
        host = normalize_host(args.host)
        if args.base_port < 1 or args.port_stride < 1:
            raise ConfigurationError("--base-port and --port-stride must be positive")
        devices = parse_devices(args.devices or "", args.base_port, args.port_stride)
        if args.timeout <= 0:
            raise ConfigurationError("--timeout must be greater than zero")
        if not 1 <= args.progress_step <= 100:
            raise ConfigurationError("--progress-step must be between 1 and 100")
        if args.overwrite and not args.execute:
            raise ConfigurationError("--overwrite is only valid with --execute")
        if args.input_path and args.legacy_file:
            raise ConfigurationError("Use either --path or legacy --file, not both")

        if args.check:
            clients = [
                MytClient(
                    host,
                    device,
                    request_timeout=args.timeout,
                    verbose=args.verbose,
                )
                for device in devices
            ]
            with ThreadPoolExecutor(max_workers=min(8, len(clients))) as executor:
                results = list(executor.map(run_check, clients))
            print_results(results, json_output=args.json)
            return 0 if all(item["status"] == "ok" for item in results) else 1

        input_path = validate_input_path(args.input_path or args.legacy_file or "")
        remote_dir = validate_remote_dir(args.remote_dir)
        entries = collect_input_files(input_path, args.remote_name)
        workers = args.workers or min(8, len(devices))
        if not 1 <= workers <= 32:
            raise ConfigurationError("--workers must be between 1 and 32")

        mode = "EXECUTE" if args.execute else "PREVIEW"
        log(f"Mode: {mode}")
        log(f"Input path: {input_path}")
        log(
            f"Files: {len(entries)} "
            f"({human_size(sum(entry.size for entry in entries))})"
        )
        log(f"Remote directory: {remote_dir}")
        log("Devices: " + ", ".join(device.label for device in devices))
        for entry in entries[:20]:
            log(
                f"  {entry.local_path} -> "
                f"{remote_dir}/{entry.relative_remote_path}"
            )
        if len(entries) > 20:
            log(f"  ... and {len(entries) - 20} more files")

        results_by_port: dict[int, dict[str, Any]] = {}
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {}
            for device in devices:
                client = MytClient(
                    host,
                    device,
                    request_timeout=args.timeout,
                    verbose=args.verbose,
                )
                future = executor.submit(
                    upload_device,
                    client,
                    entries,
                    remote_dir,
                    execute=args.execute,
                    overwrite=args.overwrite,
                    progress_step=args.progress_step,
                )
                futures[future] = device
            for future in as_completed(futures):
                result = future.result()
                results_by_port[int(result["port"])] = result
        results = [results_by_port[device.port] for device in devices]
        print_results(results, json_output=args.json)
        good = {"preview", "ok"}
        return 0 if all(item["status"] in good for item in results) else 1
    except ConfigurationError as exc:
        parser.error(str(exc))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
