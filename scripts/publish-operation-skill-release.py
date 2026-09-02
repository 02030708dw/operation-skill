#!/usr/bin/env python3
"""Publish a built Operation Skills release to Cloudflare R2."""

from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dist", type=Path, required=True)
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parent.parent)
    parser.add_argument("--prefix", default="operation-skills")
    parser.add_argument(
        "--verify-only",
        action="store_true",
        help="verify manifest-bound local artifacts without loading R2 dependencies or credentials",
    )
    return parser.parse_args()


def required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ValueError(f"missing environment variable: {name}")
    return value


def upload(client, bucket: str, source: Path, key: str, cache_control: str) -> None:
    content_type = mimetypes.guess_type(source.name)[0] or "application/octet-stream"
    client.upload_file(
        str(source),
        bucket,
        key,
        ExtraArgs={"ContentType": content_type, "CacheControl": cache_control},
    )
    print(f"uploaded {key}")


def release_identity(manifest: Any, label: str) -> tuple[int, str]:
    if not isinstance(manifest, dict):
        raise ValueError(f"{label} manifest must be a JSON object")
    sequence = manifest.get("releaseSequence")
    if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence <= 0:
        raise ValueError(f"{label} manifest releaseSequence must be a positive integer")
    commit = manifest.get("commit")
    if not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError(f"{label} manifest commit must be a full lowercase Git SHA")
    return sequence, commit


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def artifact_identity(
    manifest: dict[str, Any], name: str
) -> tuple[str, int, str]:
    entry = manifest.get(name)
    if not isinstance(entry, dict):
        raise ValueError(f"manifest {name} must be an object")
    url = entry.get("url")
    if not isinstance(url, str):
        raise ValueError(f"manifest {name}.url must be a string")
    parsed_url = urlsplit(url)
    filename = Path(parsed_url.path).name
    if parsed_url.scheme != "https" or not parsed_url.netloc or not filename:
        raise ValueError(f"manifest {name}.url must be an HTTPS artifact URL")
    size = entry.get("size")
    if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
        raise ValueError(f"manifest {name}.size must be a positive integer")
    sha256 = entry.get("sha256")
    if not isinstance(sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", sha256):
        raise ValueError(f"manifest {name}.sha256 must be a lowercase SHA-256 digest")
    return filename, size, sha256


def verify_file(path: Path, label: str, expected_size: int, expected_sha256: str) -> None:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"local {label} is missing or is not a regular file: {path}")
    actual_size = path.stat().st_size
    if actual_size != expected_size:
        raise ValueError(
            f"local {label} size mismatch: expected {expected_size}, got {actual_size}"
        )
    actual_sha256 = file_hash(path)
    if actual_sha256 != expected_sha256:
        raise ValueError(
            f"local {label} SHA-256 mismatch: expected {expected_sha256}, got {actual_sha256}"
        )


def verify_release_artifacts(
    dist: Path, repo_root: Path
) -> tuple[dict[str, Any], Path, Path, Path, str, str]:
    manifest_path = dist / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"local manifest is unreadable or invalid JSON: {exc}") from exc
    _, commit = release_identity(manifest, "local")

    archive_name, archive_size, archive_sha256 = artifact_identity(manifest, "archive")
    expected_archive_name = f"operation-skills-{commit}.zip"
    if archive_name != expected_archive_name:
        raise ValueError(
            f"manifest archive URL must end with {expected_archive_name}, got {archive_name}"
        )
    archive_path = dist / archive_name
    verify_file(archive_path, "archive", archive_size, archive_sha256)

    updater_name, updater_size, updater_sha256 = artifact_identity(manifest, "updater")
    expected_updater_name = f"operation_skill_updater-{updater_sha256}.py"
    if updater_name != expected_updater_name:
        raise ValueError(
            f"manifest updater URL must end with {expected_updater_name}, got {updater_name}"
        )
    updater_path = repo_root / "scripts" / "operation_skill_updater.py"
    verify_file(updater_path, "updater", updater_size, updater_sha256)

    print(
        "verified local release artifacts: "
        f"archive={archive_name} ({archive_size} bytes), "
        f"updater={updater_name} ({updater_size} bytes)"
    )
    return manifest, manifest_path, archive_path, updater_path, archive_name, updater_name


def is_missing_key(error: Exception) -> bool:
    response = getattr(error, "response", None)
    if not isinstance(response, dict):
        return False
    details = response.get("Error", {})
    code = str(details.get("Code", "")) if isinstance(details, dict) else ""
    metadata = response.get("ResponseMetadata", {})
    status = metadata.get("HTTPStatusCode") if isinstance(metadata, dict) else None
    return code in {"NoSuchKey", "404"} or status == 404


def remote_manifest(client, bucket: str, key: str) -> dict[str, Any] | None:
    try:
        response = client.get_object(Bucket=bucket, Key=key)
    except Exception as exc:
        if is_missing_key(exc):
            return None
        raise
    body = response.get("Body")
    if body is None or not hasattr(body, "read"):
        raise ValueError("remote manifest response has no readable body")
    try:
        payload = body.read()
    finally:
        close = getattr(body, "close", None)
        if callable(close):
            close()
    try:
        decoded = json.loads(payload)
    except (TypeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"remote manifest is not valid JSON: {exc}") from exc
    if not isinstance(decoded, dict):
        raise ValueError("remote manifest must be a JSON object")
    return decoded


def ensure_publish_is_current(
    client, bucket: str, key: str, local_manifest: dict[str, Any]
) -> None:
    local_sequence, local_commit = release_identity(local_manifest, "local")
    remote = remote_manifest(client, bucket, key)
    if remote is None:
        print(f"no existing {key}; publishing first release")
        return
    remote_sequence, remote_commit = release_identity(remote, "remote")
    if remote_sequence > local_sequence:
        raise ValueError(
            "refusing to publish older release sequence "
            f"{local_sequence}; remote stable manifest is {remote_sequence}"
        )
    if remote_sequence == local_sequence and remote_commit != local_commit:
        raise ValueError(
            "refusing to reuse release sequence "
            f"{local_sequence} for commit {local_commit}; remote commit is {remote_commit}"
        )
    if remote_sequence == local_sequence:
        print(
            f"release sequence {local_sequence} for commit {local_commit} already published; "
            "continuing idempotent publish"
        )


def main() -> int:
    args = parse_args()
    dist = args.dist.expanduser().resolve()
    repo_root = args.repo_root.expanduser().resolve()
    (
        manifest,
        manifest_path,
        archive_path,
        updater_path,
        archive_name,
        updater_name,
    ) = verify_release_artifacts(dist, repo_root)
    if args.verify_only:
        return 0

    try:
        import boto3
    except ImportError as exc:
        raise RuntimeError("boto3 is required to publish the release") from exc

    account_id = required_env("CLOUDFLARE_R2_ACCOUNT_ID")
    bucket = required_env("CLOUDFLARE_R2_BUCKET")
    client = boto3.client(
        "s3",
        endpoint_url=f"https://{account_id}.r2.cloudflarestorage.com",
        aws_access_key_id=required_env("CLOUDFLARE_R2_ACCESS_KEY_ID"),
        aws_secret_access_key=required_env("CLOUDFLARE_R2_SECRET_ACCESS_KEY"),
        region_name="auto",
    )
    prefix = args.prefix.strip("/")
    manifest_key = f"{prefix}/stable/manifest.json"

    # Guard the whole publish before writing even immutable objects. This also
    # makes a rerun of the same workflow idempotent while blocking stale jobs.
    ensure_publish_is_current(client, bucket, manifest_key, manifest)

    # Publish immutable content first. The stable manifest is the final pointer.
    upload(client, bucket, archive_path, f"{prefix}/releases/{archive_name}", "public, max-age=31536000, immutable")
    upload(client, bucket, updater_path, f"{prefix}/releases/{updater_name}", "public, max-age=31536000, immutable")
    for name in (
        "operation_skill_updater.py",
        "install-operation-skill-updater.ps1",
        "install-operation-skill-updater.sh",
    ):
        upload(client, bucket, repo_root / "scripts" / name, f"{prefix}/stable/{name}", "no-cache")
    upload(client, bucket, manifest_path, manifest_key, "no-store")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
