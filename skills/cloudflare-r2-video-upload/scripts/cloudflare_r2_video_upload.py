#!/usr/bin/env python3
"""Upload local video files to Cloudflare R2 with safe Hermes defaults."""

from __future__ import annotations

import argparse
import json
import math
import mimetypes
import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
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


@dataclass
class UploadResult:
    file: str
    key: str
    size: int
    status: str
    message: str = ""
    etag: str = ""
    url: str = ""


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
            return UploadResult(str(item.local_path), item.key, item.size, "ready", url=public_url(base_url, item.key))
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
            )
        return UploadResult(
            str(item.local_path),
            item.key,
            item.size,
            "conflict",
            f"remote size is {remote_size}; use --overwrite only if replacement is intended",
            str(remote.get("ETag", "")).strip('"'),
            public_url(base_url, item.key),
        )
    except Exception as exc:
        return UploadResult(str(item.local_path), item.key, item.size, "failed", str(exc))


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
            )
        return UploadResult(
            str(item.local_path),
            item.key,
            item.size,
            "uploaded",
            etag=str(remote.get("ETag", "")).strip('"'),
            url=public_url(base_url, item.key),
        )
    except Exception as exc:
        return UploadResult(str(item.local_path), item.key, item.size, "failed", str(exc))


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


def write_report(args: argparse.Namespace, endpoint: str, results: list[UploadResult]) -> tuple[Path, Path]:
    report_dir = args.report_dir.expanduser().resolve()
    report_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    json_path = report_dir / f"{stamp}.json"
    markdown_path = report_dir / f"{stamp}.md"
    summary: dict[str, int] = {}
    for result in results:
        summary[result.status] = summary.get(result.status, 0) + 1
    payload = {
        "time": datetime.now().astimezone().isoformat(timespec="seconds"),
        "bucket": args.bucket,
        "endpoint": endpoint,
        "source": str(args.source.expanduser().resolve()),
        "prefix": normalize_prefix(args.prefix),
        "summary": summary,
        "results": [{**asdict(result), "file": result.file} for result in results],
    }
    json_text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    lines = [
        "# Cloudflare R2 Video Upload Report",
        "",
        f"- Time: {payload['time']}",
        f"- Bucket: `{args.bucket}`",
        f"- Source: `{payload['source']}`",
        f"- Prefix: `{payload['prefix']}`",
        f"- Summary: `{json.dumps(summary, ensure_ascii=False)}`",
        "",
        "| Status | Size | Object key |",
        "|---|---:|---|",
    ]
    for result in results:
        safe_key = result.key.replace("|", "\\|")
        lines.append(f"| {result.status} | {human_size(result.size)} | `{safe_key}` |")
    markdown_text = "\n".join(lines) + "\n"
    json_path.write_text(json_text, encoding="utf-8")
    markdown_path.write_text(markdown_text, encoding="utf-8")
    (report_dir / "latest.json").write_text(json_text, encoding="utf-8")
    (report_dir / "latest.md").write_text(markdown_text, encoding="utf-8")
    return markdown_path, json_path


def run(args: argparse.Namespace) -> int:
    if not args.bucket:
        raise ValueError("Set CLOUDFLARE_R2_BUCKET or pass --bucket")
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
    if not args.source:
        raise ValueError("--source is required unless --check is used")

    items = discover_files(
        args.source,
        args.prefix,
        all_files=args.all_files,
        include_hidden=args.include_hidden,
        flatten=args.flatten,
        count=args.count,
    )
    if not items:
        print("No matching files found.")
        return 0

    env = required_environment()
    print("Mode:", "EXECUTE" if args.execute else "DRY RUN")
    print("Bucket:", args.bucket)
    print("Files:", len(items))
    print("Bytes:", human_size(sum(item.size for item in items)))
    print("Workers:", args.workers)

    if not args.execute:
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = {
                pool.submit(inspect_item, client, args.bucket, item, env["public_base_url"]): item
                for item in items
            }
            results = [future.result() for future in as_completed(futures)]
        results.sort(key=lambda result: result.key.casefold())
        print_results(results, execute=False)
        return 1 if any(result.status == "failed" for result in results) else 0

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
    markdown_path, json_path = write_report(args, endpoint, results)
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
