#!/usr/bin/env python3
"""Upload local video files to Cloudflare R2 with safe Hermes defaults."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import mimetypes
import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path, PurePosixPath
from urllib.parse import quote

try:
    import boto3
    from boto3.s3.transfer import TransferConfig
    from botocore.config import Config
    from botocore.exceptions import ClientError
except ImportError:
    boto3 = None
    TransferConfig = None
    Config = None
    ClientError = Exception


SKILL_NAME = "cloudflare-r2-video-upload"
VIDEO_EXTENSIONS = {
    ".mp4",
    ".mov",
    ".mkv",
    ".webm",
    ".avi",
    ".m4v",
    ".flv",
    ".wmv",
    ".ts",
    ".mts",
    ".m2ts",
}
PRINT_LOCK = threading.Lock()


@dataclass(frozen=True)
class UploadItem:
    local_path: Path
    key: str
    size: int
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass
class UploadResult:
    file: str
    key: str
    size: int
    status: str
    message: str = ""
    etag: str = ""
    url: str = ""
    metadata: dict[str, object] = field(default_factory=dict)


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than 0")
    return parsed


def non_negative_int(value: str) -> int:
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be 0 or greater")
    return parsed


def multipart_chunk_mib(value: str) -> int:
    parsed = int(value)
    if parsed < 5:
        raise argparse.ArgumentTypeError("must be at least 5 MiB for Cloudflare R2 multipart uploads")
    return parsed


def infer_hermes_home() -> Path:
    configured = os.environ.get("HERMES_HOME")
    if configured:
        return Path(configured).expanduser()
    skill_dir = Path(__file__).resolve().parent.parent
    if skill_dir.parent.name.lower() == "skills":
        return skill_dir.parent.parent
    return Path.home() / ".hermes"


def build_parser() -> argparse.ArgumentParser:
    state_dir = infer_hermes_home() / SKILL_NAME
    parser = argparse.ArgumentParser(
        description=(
            "Upload videos to Cloudflare R2. Default mode is a read-only preview; "
            "actual uploads require --execute."
        )
    )
    parser.add_argument("--source", type=Path, help="video file or directory")
    parser.add_argument(
        "--manifest",
        type=Path,
        help="download-result JSON manifest; only its downloaded local files are selected",
    )
    parser.add_argument("--bucket", default=os.environ.get("CLOUDFLARE_R2_BUCKET"))
    parser.add_argument("--prefix", default=os.environ.get("CLOUDFLARE_R2_PREFIX", ""))
    parser.add_argument("--endpoint", default=os.environ.get("CLOUDFLARE_R2_ENDPOINT"))
    parser.add_argument("--count", type=non_negative_int, default=0, help="maximum files; 0 means all")
    parser.add_argument("--workers", type=positive_int, default=3, help="parallel files")
    parser.add_argument("--part-workers", type=positive_int, default=4, help="parallel multipart pieces per file")
    parser.add_argument("--multipart-threshold-mib", type=positive_int, default=64)
    parser.add_argument("--multipart-chunk-mib", type=multipart_chunk_mib, default=16)
    parser.add_argument("--all-files", action="store_true", help="include non-video files")
    parser.add_argument("--include-hidden", action="store_true")
    parser.add_argument("--flatten", action="store_true", help="discard relative directory names")
    parser.add_argument("--overwrite", action="store_true", help="replace a different-size object")
    parser.add_argument("--execute", action="store_true", help="perform uploads")
    parser.add_argument("--check", action="store_true", help="validate credentials and bucket access")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--json", action="store_true", help="print the final result envelope as JSON")
    parser.add_argument("--result-json", type=Path, help="write the final result envelope to this path")
    parser.add_argument("--execution-id", help="backend execution identifier copied into the result")
    parser.add_argument(
        "--report-dir",
        type=Path,
        default=Path(os.environ.get("CLOUDFLARE_R2_REPORTS", state_dir / "reports")),
    )
    return parser


def normalize_prefix(prefix: str) -> str:
    clean = str(PurePosixPath(prefix.replace("\\", "/"))).strip("/")
    return "" if clean == "." else clean


def is_hidden(relative: Path) -> bool:
    return any(part.startswith(".") for part in relative.parts)


def discover_files(
    source: Path,
    prefix: str,
    *,
    all_files: bool,
    include_hidden: bool,
    flatten: bool,
    count: int,
) -> list[UploadItem]:
    source = source.expanduser().resolve()
    if not source.exists():
        raise ValueError(f"Source does not exist: {source}")
    if source.is_file():
        candidates = [(source, Path(source.name))]
    elif source.is_dir():
        candidates = [
            (path, path.relative_to(source))
            for path in source.rglob("*")
            if path.is_file()
        ]
    else:
        raise ValueError(f"Source is not a regular file or directory: {source}")

    selected: list[UploadItem] = []
    keys: dict[str, Path] = {}
    clean_prefix = normalize_prefix(prefix)
    for path, relative in sorted(candidates, key=lambda pair: pair[1].as_posix().casefold()):
        if not include_hidden and is_hidden(relative):
            continue
        if not all_files and path.suffix.lower() not in VIDEO_EXTENSIONS:
            continue
        object_relative = Path(path.name) if flatten else relative
        relative_key = object_relative.as_posix().lstrip("/")
        key = f"{clean_prefix}/{relative_key}" if clean_prefix else relative_key
        if not key or key == "." or ".." in PurePosixPath(key).parts:
            raise ValueError(f"Unsafe object key generated for: {path}")
        if key in keys:
            raise ValueError(f"Object-key collision: {keys[key]} and {path} -> {key}")
        keys[key] = path
        selected.append(UploadItem(path, key, path.stat().st_size))
        if count and len(selected) >= count:
            break
    return selected


def safe_key_segment(value: object, fallback: str) -> str:
    text = str(value or "").strip().replace("\\", "_").replace("/", "_")
    text = "".join(char if char.isalnum() or char in {"-", "_", ".", " "} else "_" for char in text)
    return text.strip(" .")[:120] or fallback


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _manifest_videos(payload: object) -> list[dict[str, object]]:
    if not isinstance(payload, dict):
        raise ValueError("Manifest root must be a JSON object")
    videos: list[dict[str, object]] = []
    for source in payload.get("sources", []):
        if not isinstance(source, dict):
            continue
        source_name = source.get("name")
        source_url = source.get("url")
        for raw in source.get("videos", []):
            if isinstance(raw, dict):
                item = dict(raw)
                item.setdefault("source", source_name)
                item.setdefault("sourceUrl", source_url)
                videos.append(item)
    for key in ("videos", "files", "items"):
        for raw in payload.get(key, []):
            if isinstance(raw, dict):
                videos.append(dict(raw))
    return videos


def discover_manifest(
    manifest: Path,
    prefix: str,
    count: int,
    *,
    flatten: bool = False,
) -> list[UploadItem]:
    path = manifest.expanduser().resolve()
    if not path.is_file():
        raise ValueError(f"Manifest does not exist: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Cannot read manifest {path}: {exc}") from exc
    selected: list[UploadItem] = []
    keys: dict[str, Path] = {}
    clean_prefix = normalize_prefix(prefix)
    for raw in _manifest_videos(payload):
        status = str(raw.get("status") or "").lower()
        if status not in {"downloaded", "ready", "upload-failed", "conflict"}:
            continue
        local_value = raw.get("localPath") or raw.get("local_path") or raw.get("file")
        if not local_value:
            continue
        local_path = Path(str(local_value)).expanduser().resolve()
        if not local_path.is_file():
            raise ValueError(f"Manifest local file does not exist: {local_path}")
        expected_size = raw.get("fileSize") or raw.get("file_size") or raw.get("size")
        actual_size = local_path.stat().st_size
        if expected_size is not None and int(expected_size) != actual_size:
            raise ValueError(
                f"Manifest size mismatch for {local_path}: expected {expected_size}, actual {actual_size}"
            )
        expected_sha256 = raw.get("sha256") or raw.get("fileSha256") or raw.get("file_sha256")
        if expected_sha256 is not None:
            normalized_sha256 = str(expected_sha256).strip().lower()
            if len(normalized_sha256) != 64 or any(
                character not in "0123456789abcdef" for character in normalized_sha256
            ):
                raise ValueError(f"Manifest SHA-256 is invalid for {local_path}")
            actual_sha256 = sha256_file(local_path)
            if actual_sha256 != normalized_sha256:
                raise ValueError(
                    f"Manifest SHA-256 mismatch for {local_path}: "
                    f"expected {normalized_sha256}, actual {actual_sha256}"
                )
        source = safe_key_segment(raw.get("source"), "source")
        file_name = safe_key_segment(raw.get("fileName") or local_path.name, local_path.name)
        explicit_key = raw.get("r2ObjectKey") or raw.get("objectKey")
        if explicit_key:
            relative_key = str(explicit_key).strip("/")
        elif flatten:
            relative_key = file_name
        else:
            relative_key = f"{source}/{file_name}"
        key = f"{clean_prefix}/{relative_key}" if clean_prefix else relative_key
        if not key or ".." in PurePosixPath(key).parts:
            raise ValueError(f"Unsafe object key in manifest: {key}")
        if key in keys:
            raise ValueError(f"Object-key collision: {keys[key]} and {local_path} -> {key}")
        keys[key] = local_path
        metadata = {
            "platform": raw.get("platform"),
            "source": raw.get("source"),
            "sourceUrl": raw.get("sourceUrl"),
            "platformVideoId": raw.get("platformVideoId"),
            "originalUrl": raw.get("originalUrl"),
            "canonicalUrl": raw.get("canonicalUrl"),
            "fileName": raw.get("fileName") or local_path.name,
            "sha256": expected_sha256,
        }
        selected.append(UploadItem(local_path, key, actual_size, metadata))
        if count and len(selected) >= count:
            break
    return selected


def required_environment() -> dict[str, str | None]:
    return {
        "account_id": os.environ.get("CLOUDFLARE_R2_ACCOUNT_ID"),
        "access_key_id": os.environ.get("CLOUDFLARE_R2_ACCESS_KEY_ID"),
        "secret_access_key": os.environ.get("CLOUDFLARE_R2_SECRET_ACCESS_KEY"),
        "session_token": os.environ.get("CLOUDFLARE_R2_SESSION_TOKEN"),
        "public_base_url": os.environ.get("CLOUDFLARE_R2_PUBLIC_BASE_URL"),
    }


def resolved_endpoint(account_id: str | None, endpoint: str | None) -> str:
    if endpoint:
        return endpoint.rstrip("/")
    if not account_id:
        raise ValueError("Set CLOUDFLARE_R2_ACCOUNT_ID or pass --endpoint")
    return f"https://{account_id}.r2.cloudflarestorage.com"


def create_client(args: argparse.Namespace):
    if boto3 is None:
        raise ValueError(
            "Missing boto3. Install it with: python -m pip install -r scripts/requirements.txt"
        )
    env = required_environment()
    missing = [
        name
        for name, value in (
            ("CLOUDFLARE_R2_ACCESS_KEY_ID", env["access_key_id"]),
            ("CLOUDFLARE_R2_SECRET_ACCESS_KEY", env["secret_access_key"]),
        )
        if not value
    ]
    if missing:
        raise ValueError(f"Missing environment variable(s): {', '.join(missing)}")
    endpoint = resolved_endpoint(env["account_id"], args.endpoint)
    return boto3.client(
        service_name="s3",
        endpoint_url=endpoint,
        aws_access_key_id=env["access_key_id"],
        aws_secret_access_key=env["secret_access_key"],
        aws_session_token=env["session_token"],
        region_name="auto",
        config=Config(
            signature_version="s3v4",
            retries={"max_attempts": 6, "mode": "adaptive"},
            connect_timeout=20,
            read_timeout=120,
        ),
    )


def client_error_code(error: BaseException) -> str:
    if isinstance(error, ClientError):
        return str(error.response.get("Error", {}).get("Code", ""))
    return ""


def remote_metadata(client, bucket: str, key: str) -> dict | None:
    try:
        return client.head_object(Bucket=bucket, Key=key)
    except ClientError as exc:
        if client_error_code(exc) in {"404", "NoSuchKey", "NotFound"}:
            return None
        raise


def public_url(base_url: str | None, key: str) -> str:
    if not base_url:
        return ""
    return f"{base_url.rstrip('/')}/{quote(key, safe='/')}"


def inspect_item(client, bucket: str, item: UploadItem, base_url: str | None) -> UploadResult:
    try:
        remote = remote_metadata(client, bucket, item.key)
        if remote is None:
            return UploadResult(
                str(item.local_path), item.key, item.size, "ready",
                url=public_url(base_url, item.key), metadata=item.metadata,
            )
        remote_size = int(remote.get("ContentLength", -1))
        if remote_size == item.size:
            return UploadResult(
                str(item.local_path),
                item.key,
                item.size,
                "skipped-existing",
                "remote object has the same size",
                str(remote.get("ETag", "")).strip('"'),
                public_url(base_url, item.key),
                item.metadata,
            )
        return UploadResult(
            str(item.local_path),
            item.key,
            item.size,
            "conflict",
            f"remote size is {remote_size}; use --overwrite only if replacement is intended",
            str(remote.get("ETag", "")).strip('"'),
            public_url(base_url, item.key),
            item.metadata,
        )
    except Exception as exc:
        return UploadResult(
            str(item.local_path), item.key, item.size, "failed", str(exc),
            metadata=item.metadata,
        )


def upload_item(
    client,
    bucket: str,
    item: UploadItem,
    base_url: str | None,
    transfer_config,
    overwrite: bool,
    verbose: bool,
) -> UploadResult:
    inspected = inspect_item(client, bucket, item, base_url)
    if inspected.status == "skipped-existing":
        return inspected
    if inspected.status == "failed":
        return inspected
    if inspected.status == "conflict" and not overwrite:
        return inspected

    content_type = mimetypes.guess_type(item.local_path.name)[0] or "application/octet-stream"
    progress_lock = threading.Lock()
    progress = 0
    next_percent = 25

    def callback(bytes_amount: int) -> None:
        nonlocal progress, next_percent
        if not verbose or item.size <= 0:
            return
        with progress_lock:
            progress += bytes_amount
            percent = min(100, int(progress * 100 / item.size))
            if percent >= next_percent:
                with PRINT_LOCK:
                    print(f"  progress {percent:3d}%  {item.key}", flush=True)
                next_percent += 25

    try:
        client.upload_file(
            str(item.local_path),
            bucket,
            item.key,
            ExtraArgs={"ContentType": content_type},
            Config=transfer_config,
            Callback=callback,
        )
        remote = client.head_object(Bucket=bucket, Key=item.key)
        remote_size = int(remote.get("ContentLength", -1))
        if remote_size != item.size:
            return UploadResult(
                str(item.local_path),
                item.key,
                item.size,
                "failed",
                f"verification failed: remote size {remote_size}",
                metadata=item.metadata,
            )
        return UploadResult(
            str(item.local_path),
            item.key,
            item.size,
            "uploaded",
            etag=str(remote.get("ETag", "")).strip('"'),
            url=public_url(base_url, item.key),
            metadata=item.metadata,
        )
    except Exception as exc:
        return UploadResult(
            str(item.local_path), item.key, item.size, "failed", str(exc),
            metadata=item.metadata,
        )


def human_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{size} B"


def print_results(results: list[UploadResult], execute: bool) -> None:
    print("\nResult")
    for result in results:
        print(f"- {result.status:16s} {human_size(result.size):>10s}  {result.key}")
        if result.message:
            print(f"  {result.message}")
        if result.url:
            print(f"  {result.url}")
    counts: dict[str, int] = {}
    for result in results:
        counts[result.status] = counts.get(result.status, 0) + 1
    print(json.dumps({"mode": "execute" if execute else "dry-run", "counts": counts}, ensure_ascii=False))


def upload_result_payload(result: UploadResult, bucket: str) -> dict[str, object]:
    payload = dict(result.metadata)
    payload.update(
        {
            "localPath": result.file,
            "fileSize": result.size,
            "r2Bucket": bucket,
            "r2ObjectKey": result.key,
            "r2Url": result.url or None,
            "etag": result.etag or None,
            "status": result.status,
            "error": result.message or None,
        }
    )
    return payload


def result_status(results: list[UploadResult], execute: bool) -> str:
    bad = sum(result.status in {"failed", "conflict"} for result in results)
    good_statuses = {"uploaded", "skipped-existing"} if execute else {"ready", "skipped-existing"}
    good = sum(result.status in good_statuses for result in results)
    if bad == 0:
        return "completed" if execute else "preview"
    return "partial" if good else "failed"


def build_result_payload(
    args: argparse.Namespace,
    endpoint: str,
    results: list[UploadResult],
) -> dict[str, object]:
    summary: dict[str, int] = {}
    for result in results:
        summary[result.status] = summary.get(result.status, 0) + 1
    return {
        "schemaVersion": "1.0",
        "skill": SKILL_NAME,
        "executionId": args.execution_id,
        "mode": "execute" if args.execute else "dry-run",
        "status": result_status(results, args.execute),
        "time": datetime.now().astimezone().isoformat(timespec="seconds"),
        "bucket": args.bucket,
        "endpoint": endpoint,
        "source": str(args.source.expanduser().resolve()) if args.source else None,
        "manifest": str(args.manifest.expanduser().resolve()) if args.manifest else None,
        "prefix": normalize_prefix(args.prefix),
        "summary": summary,
        "videos": [upload_result_payload(result, args.bucket) for result in results],
    }


def write_result_json(path: Path, payload: dict[str, object]) -> Path:
    resolved = path.expanduser().resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return resolved


def write_report(args: argparse.Namespace, payload: dict[str, object]) -> tuple[Path, Path]:
    report_dir = args.report_dir.expanduser().resolve()
    report_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    json_path = report_dir / f"{stamp}.json"
    markdown_path = report_dir / f"{stamp}.md"
    json_text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    lines = [
        "# Cloudflare R2 Video Upload Report",
        "",
        f"- Time: {payload['time']}",
        f"- Bucket: `{args.bucket}`",
        f"- Source: `{payload.get('source') or payload.get('manifest')}`",
        f"- Prefix: `{payload['prefix']}`",
        f"- Summary: `{json.dumps(payload['summary'], ensure_ascii=False)}`",
        "",
        "| Status | Size | Object key |",
        "|---|---:|---|",
    ]
    for result in payload["videos"]:
        safe_key = str(result["r2ObjectKey"]).replace("|", "\\|")
        lines.append(
            f"| {result['status']} | {human_size(int(result['fileSize']))} | `{safe_key}` |"
        )
    markdown_text = "\n".join(lines) + "\n"
    json_path.write_text(json_text, encoding="utf-8")
    markdown_path.write_text(markdown_text, encoding="utf-8")
    (report_dir / "latest.json").write_text(json_text, encoding="utf-8")
    (report_dir / "latest.md").write_text(markdown_text, encoding="utf-8")
    return markdown_path, json_path


def run(args: argparse.Namespace) -> int:
    if not args.bucket:
        raise ValueError("Set CLOUDFLARE_R2_BUCKET or pass --bucket")
    if args.source and args.manifest:
        raise ValueError("Use either --source or --manifest, not both")
    client = create_client(args)
    endpoint = resolved_endpoint(required_environment()["account_id"], args.endpoint)
    try:
        client.head_bucket(Bucket=args.bucket)
    except Exception as exc:
        raise ValueError(f"Cannot access bucket {args.bucket}: {exc}") from exc

    if args.check:
        print(
            json.dumps(
                {
                    "status": "ok",
                    "bucket": args.bucket,
                    "endpoint": endpoint,
                    "credentials": "configured",
                    "boto3": getattr(boto3, "__version__", "installed"),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if not args.source and not args.manifest:
        raise ValueError("--source or --manifest is required unless --check is used")

    if args.manifest:
        items = discover_manifest(
            args.manifest,
            args.prefix,
            args.count,
            flatten=args.flatten,
        )
    else:
        items = discover_files(
            args.source,
            args.prefix,
            all_files=args.all_files,
            include_hidden=args.include_hidden,
            flatten=args.flatten,
            count=args.count,
        )

    env = required_environment()
    print("Mode:", "EXECUTE" if args.execute else "DRY RUN")
    print("Bucket:", args.bucket)
    print("Files:", len(items))
    print("Bytes:", human_size(sum(item.size for item in items)))
    print("Workers:", args.workers)

    results: list[UploadResult]
    if not args.execute:
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = {
                pool.submit(inspect_item, client, args.bucket, item, env["public_base_url"]): item
                for item in items
            }
            results = [future.result() for future in as_completed(futures)]
        results.sort(key=lambda result: result.key.casefold())
        print_results(results, execute=False)
        payload = build_result_payload(args, endpoint, results)
        if args.result_json:
            write_result_json(args.result_json, payload)
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 1 if any(result.status == "failed" for result in results) else 0

    if not items:
        print("No matching files found.")
        payload = build_result_payload(args, endpoint, [])
        if args.result_json:
            write_result_json(args.result_json, payload)
        markdown_path, json_path = write_report(args, payload)
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        print(f"Report: {markdown_path}")
        print(f"JSON: {json_path}")
        return 0

    largest_file = max(item.size for item in items)
    minimum_for_part_limit = math.ceil(largest_file / (10_000 * 1024 * 1024))
    effective_chunk_mib = max(args.multipart_chunk_mib, minimum_for_part_limit, 5)
    if effective_chunk_mib > 5 * 1024:
        raise ValueError("A selected file exceeds Cloudflare R2 multipart limits")
    if args.verbose:
        print("Multipart chunk:", f"{effective_chunk_mib} MiB")
    transfer_config = TransferConfig(
        multipart_threshold=args.multipart_threshold_mib * 1024 * 1024,
        multipart_chunksize=effective_chunk_mib * 1024 * 1024,
        max_concurrency=args.part_workers,
        use_threads=True,
    )
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {
            pool.submit(
                upload_item,
                client,
                args.bucket,
                item,
                env["public_base_url"],
                transfer_config,
                args.overwrite,
                args.verbose,
            ): item
            for item in items
        }
        results = []
        for future in as_completed(futures):
            result = future.result()
            results.append(result)
            with PRINT_LOCK:
                print(f"{result.status}: {result.key}", flush=True)
    results.sort(key=lambda result: result.key.casefold())
    print_results(results, execute=True)
    payload = build_result_payload(args, endpoint, results)
    if args.result_json:
        write_result_json(args.result_json, payload)
    markdown_path, json_path = write_report(args, payload)
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    print(f"Report: {markdown_path}")
    print(f"JSON: {json_path}")
    return 1 if any(result.status in {"failed", "conflict"} for result in results) else 0


def main() -> int:
    args = build_parser().parse_args()
    return run(args)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(2)
