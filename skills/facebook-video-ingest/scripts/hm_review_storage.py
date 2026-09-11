"""Encrypted review storage. Secrets stay in a mounted regional key file, never in journals."""
from __future__ import annotations
import base64
import contextlib
import hashlib
import json
import os
from pathlib import Path
import threading
from urllib.parse import quote


class StorageFailure(RuntimeError):
    pass


def configuration(region: str) -> dict:
    if region not in {"PH", "TH", "VN", "ID"} or region != os.environ.get("HM_R2_KEY_PREFIX"):
        raise StorageFailure("REGION_MISMATCH")
    path = Path(os.environ.get("HM_REVIEW_KEY_FILE", "/missing-review-key"))
    if path.stat().st_mode & 0o077:
        raise StorageFailure("KEY_FILE_PERMISSIONS")
    config = json.loads(path.read_text())
    if config.get("region") != region or not config.get("publicReadDenied"):
        raise StorageFailure("REVIEW_PROTECTION_UNVERIFIED")
    return config


def encryption(config: dict, version: str, source: bool = False) -> dict:
    try:
        value = base64.b64decode(config["keys"][version], validate=True)
        if len(value) != 32:
            raise ValueError()
    except (KeyError, ValueError):
        raise StorageFailure("KEY_VERSION_UNAVAILABLE") from None
    prefix = "CopySourceSSECustomer" if source else "SSECustomer"
    return {prefix + "Algorithm": "AES256", prefix + "Key": value}


def client():
    import boto3
    from botocore.config import Config
    s3 = boto3.client("s3", endpoint_url="https://" + os.environ["CLOUDFLARE_R2_ACCOUNT_ID"] + ".r2.cloudflarestorage.com",
                        aws_access_key_id=os.environ["CLOUDFLARE_R2_ACCESS_KEY_ID"],
                        aws_secret_access_key=os.environ["CLOUDFLARE_R2_SECRET_ACCESS_KEY"], region_name="auto",
                        config=Config(signature_version="s3v4", retries={"max_attempts": 2, "mode": "standard"}, connect_timeout=15, read_timeout=60))
    # R2 checks destination atomically, including a competing writer after our HEAD.
    s3.meta.events.register('before-call.s3.CopyObject', protect_copy_destination)
    return s3


def protect_copy_destination(params, **kwargs):
    params.setdefault('headers', {})['cf-copy-destination-if-none-match'] = '*'



def require_review_key(region: str, key: str):
    if not key.startswith("review/" + region + "/") or "\\" in key or any(p in {"", ".", ".."} for p in key.split("/")):
        raise StorageFailure("REVIEW_KEY_OUTSIDE_REGION")


def head(s3, bucket: str, key: str, extra: dict) -> dict | None:
    from botocore.exceptions import ClientError
    try:
        return s3.head_object(Bucket=bucket, Key=key, **extra)
    except ClientError as exc:
        if exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode") == 404:
            return None
        raise


def verify(result: dict | None, job: dict):
    if not result or result.get("ContentLength") != job["fileSize"] or result.get("Metadata", {}).get("hm-sha256") != job["fileSha256"]:
        raise StorageFailure("OBJECT_VERIFICATION_FAILED")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@contextlib.contextmanager
def lease(check):
    stopped, cancelled = threading.Event(), threading.Event()
    def heartbeat():
        while not stopped.wait(15):
            try:
                if not check().get("active"):
                    cancelled.set()
            except Exception:
                cancelled.set()
    def guard(*_):
        if cancelled.is_set():
            raise StorageFailure("LEASE_CANCELLED")
    if not check().get("active"):
        raise StorageFailure("LEASE_CANCELLED")
    thread = threading.Thread(target=heartbeat, daemon=True)
    thread.start()
    try:
        yield guard
    finally:
        stopped.set()
        thread.join(timeout=35)


def execute(job: dict, args, pipeline, guard):
    region, key = job["region"], job["objectKey"]
    require_review_key(region, key)
    config = configuration(region)
    bucket = os.environ["CLOUDFLARE_R2_BUCKET"]
    if bucket != job["bucket"]:
        raise StorageFailure("BUCKET_MISMATCH")
    extra = encryption(config, job["keyVersion"])
    s3 = client()
    guard()
    if job["kind"] == "UPLOAD":
        name = job.get("fileName") or "video.mp4"
        if "/" in name or "\\" in name or not name.endswith(".mp4"):
            raise StorageFailure("CLAIM_KEY_MISMATCH")
        expected = f"review/{region}/{job['videoNo']}/{job['jobNo']}/a{job['executionVersion']}/{name}"
        if expected != key:
            raise StorageFailure("CLAIM_KEY_MISMATCH")
        existing = head(s3, bucket, key, extra)
        if existing:
            verify(existing, job)
            return
        path = pipeline.resolve_local_delete_path(job["localPath"], args.state_dir)
        if path.stat().st_size != job["fileSize"] or sha256(path) != job["fileSha256"]:
            raise StorageFailure("LOCAL_FILE_CHANGED")
        from boto3.s3.transfer import TransferConfig
        s3.upload_file(str(path), bucket, key, ExtraArgs={**extra, "ContentType": "video/mp4", "Metadata": {"hm-sha256": job["fileSha256"]}},
                       Config=TransferConfig(max_concurrency=2), Callback=guard)
        guard()
        verify(head(s3, bucket, key, extra), job)
    elif job["kind"] == "LOCAL_DELETE":
        if not config.get("backupRestoreVerified"):
            raise StorageFailure("KEY_BACKUP_UNVERIFIED")
        path = pipeline.resolve_local_delete_path(job["localPath"], args.state_dir)
        if path.exists():
            if path.stat().st_size != job["fileSize"] or sha256(path) != job["fileSha256"]:
                raise StorageFailure("LOCAL_FILE_CHANGED")
            guard()
            path.unlink()
    elif job["kind"] == "REVIEW_DELETE":
        guard()
        s3.delete_object(Bucket=bucket, Key=key)
        if head(s3, bucket, key, extra) is not None:
            raise StorageFailure("OBJECT_DELETE_UNCONFIRMED")
    else:
        raise StorageFailure("INVALID_STORAGE_KIND")


def write_journal(path: Path, value: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    with temporary.open("w") as handle:
        json.dump(value, handle)
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)
    fd = os.open(str(path.parent), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def process_one(args, backend: str, token: str, worker_id: str, pipeline) -> bool:
    if not os.environ.get("HM_REVIEW_KEY_FILE"):
        return False
    root = args.state_dir / "review-storage"
    def call(suffix, payload):
        return pipeline.api_call(backend, token, "POST", "/api/internal/capture/review-storage/" + suffix, {"workerId": worker_id, **payload}, retry_transient=True)
    # Replay committed results before claiming new work. Files remain untouched until a separate cleanup claim.
    for journal in sorted(root.glob("*.json")):
        value = json.loads(journal.read_text())
        if value["workerId"] != worker_id:
            continue
        call(value["jobNo"] + "/complete", value["receipt"])
        journal.unlink()
    job = call("claim", {})
    if not job:
        return False
    version = {"executionVersion": job["executionVersion"]}
    receipt = {**version, "status": "SUCCEEDED", "errorCode": ""}
    try:
        with lease(lambda: call(job["jobNo"] + "/check", version)) as guard:
            execute(job, args, pipeline, guard)
    except Exception as exc:
        # Do not serialize SDK exceptions (request headers could contain SSE-C material).
        code = str(exc) if isinstance(exc, StorageFailure) else "STORAGE_IO_FAILED"
        receipt.update(status="FAILED", errorCode=code)
    journal = root / (job["jobNo"] + "-a" + str(job["executionVersion"]) + ".json")
    write_journal(journal, {"jobNo": job["jobNo"], "workerId": worker_id, "receipt": receipt})
    call(job["jobNo"] + "/complete", receipt)
    journal.unlink()
    return True


def promote(job: dict, target: str, check) -> dict:
    config = configuration(job["region"])
    source = job["reviewObjectKey"]
    require_review_key(job["region"], source)
    if not target.startswith(job["region"] + "/"):
        raise StorageFailure("FINAL_KEY_OUTSIDE_REGION")
    bucket = os.environ["CLOUDFLARE_R2_BUCKET"]
    s3 = client()
    with lease(check) as guard:
        existing = head(s3, bucket, target, {})
        if existing:
            verify(existing, job)
        else:
            verify(head(s3, bucket, source, encryption(config, job["reviewKeyVersion"])), job)
            guard()
            s3.copy_object(Bucket=bucket, Key=target, CopySource={"Bucket": bucket, "Key": source},
                           MetadataDirective="COPY", **encryption(config, job["reviewKeyVersion"], source=True))
            verify(head(s3, bucket, target, {}), job)
    return {"status": "uploaded", "r2Bucket": bucket, "r2ObjectKey": target,
            "r2Url": os.environ.get("CLOUDFLARE_R2_PUBLIC_BASE_URL", "").rstrip("/") + "/" + quote(target, safe="/")}
