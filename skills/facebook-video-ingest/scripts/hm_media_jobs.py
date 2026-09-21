"""Durable compatibility jobs. Originals and journals remain private and recoverable."""
from __future__ import annotations
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
from urllib.parse import quote, urlparse
import hm_media_compat as media
import hm_review_storage as storage


class JobFailure(RuntimeError):
    def __init__(self, code, retryable=False):
        super().__init__(code)
        self.retryable = retryable


def regional_key(key, region, review):
    prefix = f'review/{region}/' if review else f'{region}/'
    if not key or not key.startswith(prefix) or '\\' in key or any(p in ('', '.', '..') for p in key.split('/')):
        raise JobFailure('REGION_MISMATCH')


def download(s3, bucket, key, etag, extra, target, guard):
    temporary = target.with_suffix('.part')
    response = s3.get_object(Bucket=bucket, Key=key, IfMatch=etag, **extra)
    try:
        with temporary.open('wb') as output:
            for chunk in response['Body'].iter_chunks(chunk_size=1024*1024):
                guard(); output.write(chunk)
            output.flush(); os.fsync(output.fileno())
        temporary.replace(target)
    finally:
        response['Body'].close()
        temporary.unlink(missing_ok=True)


def refetch(job, base, source, guard):
    """Use the existing authorized account lease; never ingest or change metadata."""
    import hm_public_capture as capture
    candidates = job.get('videos') or []
    if not candidates:
        raise JobFailure('RECOVERY_SOURCE_UNAVAILABLE')
    video = candidates[0]
    platform, expected_id = video.get('platform'), str(video.get('platform_video_id') or '')
    url = video.get('canonical_url') or video.get('original_url') or ''
    allowed = {'Facebook': ('facebook.com', 'fb.watch'), 'YouTube': ('youtube.com', 'youtu.be'), 'TikTok': ('tiktok.com',)}
    host = (urlparse(url).hostname or '').lower()
    if not expected_id or not any(host == h or host.endswith('.'+h) for h in allowed.get(platform, ())):
        raise JobFailure('RECOVERY_IDENTITY_UNAVAILABLE')
    try: original = media.probe(source)
    except media.CompatibilityError: original = None
    expected_duration = float(video.get('duration_seconds') or 0)
    if original is None and expected_duration <= 0:
        raise JobFailure('RECOVERY_DURATION_UNAVAILABLE')
    registry = json.loads(Path(os.environ.get('HM_TENANT_CONFIG', '/opt/hm/tenants.json')).read_text())
    config = registry[job['region'].lower()]
    directory = base/'recovered'; directory.mkdir(exist_ok=True)
    guard()
    # Reuse the capture policy: only an explicit login wall permits one
    # authenticated supplement; restrictions update the same account state.
    attempts = []
    try:
        recovered = capture.attempt(
            lambda credentials: capture.download_item(platform, {'id': expected_id, 'url': url}, directory, credentials),
            config, platform, attempts, 'DOWNLOAD')
    except Exception as error:
        code = capture.classify(error)
        raise JobFailure('RECOVERY_'+code, code in ('NETWORK_ERROR','ACCOUNT_BUSY')) from None
    storage.write_journal(base/'recovery-attempts.json', {'attempts': attempts})
    guard()
    if str(recovered.get('id')) != expected_id or recovered.get('status') != 'downloaded':
        raise JobFailure('RECOVERY_IDENTITY_MISMATCH')
    candidate = Path(recovered['path']).resolve(strict=True)
    if directory.resolve() not in candidate.parents:
        raise JobFailure('RECOVERY_PATH_INVALID')
    info = media.probe(candidate)
    if original is not None: media.validate(original, info)
    elif abs(float(info['format']['duration'])-expected_duration) > max(1.0, expected_duration*.005):
        raise JobFailure('RECOVERY_DURATION_MISMATCH')
    return candidate


def execute(job, args, pipeline, guard):
    if job.get('ruleVersion') != media.VERSION:
        raise JobFailure('RULE_VERSION_MISMATCH')
    region, bucket = job['region'], job['bucket']
    config = storage.configuration(region)
    if bucket != os.environ['CLOUDFLARE_R2_BUCKET']:
        raise JobFailure('BUCKET_MISMATCH')
    no = job['jobNo']
    if not no.startswith('M-') or not no[2:].isalnum():
        raise JobFailure('INVALID_JOB_ID')
    base = Path(os.environ.get('HM_MEDIA_BACKUP_ROOT', '/opt/data/media-compat-backups'))/region/no
    base.mkdir(mode=0o700, parents=True, exist_ok=True)
    if shutil.disk_usage(base).free < max(10*1024**3, int(job['fileSize'])*4):
        raise JobFailure('MEDIA_CACHE_SPACE_LOW', True)
    storage.write_journal(base/'source.json', job)
    s3 = storage.client()
    source_key, source_etag = job.get('key'), job.get('sourceETag')
    review = not bool(job.get('catalog'))
    source_extra = storage.encryption(config, job['keyVersion']) if source_key and review else {}
    if source_key:
        regional_key(source_key, region, review)
        before = s3.head_object(Bucket=bucket, Key=source_key, **source_extra)
        if before['ETag'] != source_etag:
            raise JobFailure('SOURCE_CHANGED')
        source = base/(hashlib.sha256((source_key+source_etag).encode()).hexdigest()+'.original.mp4')
        if not source.exists():
            download(s3, bucket, source_key, source_etag, source_extra, source, guard)
    else:
        local = pipeline.resolve_local_delete_path(job['localPath'], args.state_dir)
        source = base/'local.original.mp4'
        if not source.exists():
            temporary = source.with_suffix('.part'); shutil.copyfile(local, temporary); temporary.replace(source)
    original_sha = media.sha256(source)
    if source.stat().st_size != int(job['fileSize']) or (job.get('sha256') and original_sha != job['sha256']):
        raise JobFailure('SOURCE_HASH_MISMATCH')
    recovered = False
    try:
        normalized = media.normalize(source)
    except media.CompatibilityError:
        guard()
        candidate = refetch(job, base, source, guard)
        normalized = media.normalize(candidate)
        recovered = True
    guard()
    changed = normalized['sha256'] != original_sha
    target = job['targetKey'] if changed or not source_key else source_key
    regional_key(target, region, review)
    version = job['targetKeyVersion'] if review else None
    target_extra = storage.encryption(config, version) if review else {}
    if target != source_key:
        existing = storage.head(s3, bucket, target, target_extra)
        if not existing:
            with Path(normalized['path']).open('rb') as body:
                s3.put_object(Bucket=bucket, Key=target, Body=body, IfNoneMatch='*', ContentType='video/mp4',
                    ContentDisposition="inline; filename*=UTF-8''"+quote(Path(job.get('fileName') or 'video.mp4').stem+'.mp4', safe=''),
                    Metadata={'hm-sha256': normalized['sha256'], 'hm-compat-version': str(media.VERSION)}, **target_extra)
    after = s3.head_object(Bucket=bucket, Key=target, **target_extra)
    if after['ContentLength'] != normalized['fileSize'] or (target != source_key and after.get('Metadata', {}).get('hm-sha256') != normalized['sha256']):
        raise JobFailure('TARGET_HASH_MISMATCH')
    # Verify persisted bytes, not just user-supplied object metadata.
    if target != source_key:
        verify_file = base/'verified.mp4'
        download(s3, bucket, target, after['ETag'], target_extra, verify_file, guard)
        if media.sha256(verify_file) != normalized['sha256']:
            raise JobFailure('TARGET_HASH_MISMATCH')
        verify_file.unlink()
    if source_key and s3.head_object(Bucket=bucket, Key=source_key, **source_extra)['ETag'] != source_etag:
        raise JobFailure('SOURCE_CHANGED')
    if not source_key:
        local = pipeline.resolve_local_delete_path(job['localPath'], args.state_dir)
        if media.sha256(local) != original_sha:
            raise JobFailure('SOURCE_CHANGED')
    backup_key = f"review/{region}/media-backup/{no}/source.mp4"
    backup_version = config['activeVersion']
    backup_extra = storage.encryption(config, backup_version)
    backup = storage.head(s3, bucket, backup_key, backup_extra)
    if not backup:
        if source_key:
            s3.copy_object(Bucket=bucket, Key=backup_key, CopySource={'Bucket': bucket, 'Key': source_key},
                CopySourceIfMatch=source_etag, MetadataDirective='REPLACE',
                Metadata={'hm-sha256': original_sha}, ContentType='video/mp4', **backup_extra,
                **(storage.encryption(config, job['keyVersion'], source=True) if review else {}))
        else:
            with source.open('rb') as original:
                s3.put_object(Bucket=bucket, Key=backup_key, Body=original, IfNoneMatch='*',
                    ContentType='video/mp4', Metadata={'hm-sha256': original_sha}, **backup_extra)
    backup = s3.head_object(Bucket=bucket, Key=backup_key, **backup_extra)
    if backup['ContentLength'] != source.stat().st_size or backup.get('Metadata', {}).get('hm-sha256') != original_sha:
        raise JobFailure('BACKUP_VERIFY_FAILED')
    verified_backup = base/'backup-verified.mp4'
    download(s3, bucket, backup_key, backup['ETag'], backup_extra, verified_backup, guard)
    if media.sha256(verified_backup) != original_sha:
        raise JobFailure('BACKUP_VERIFY_FAILED')
    verified_backup.unlink()
    result = dict(normalized, targetKey=target, targetETag=after['ETag'], keyVersion=version,
        sourceETag=source_etag, backupPath=str(source), backupKey=backup_key,
        backupKeyVersion=backup_version, backupETag=backup['ETag'], sourceSha256=original_sha,
        ruleVersion=media.VERSION, decoded=True,
        recovered=recovered, converted=changed)
    storage.write_journal(base/'verified.json', result)
    return result


def process_one(args, backend, token, worker_id, pipeline):
    if not os.environ.get('HM_REVIEW_KEY_FILE'):
        return False
    root = args.state_dir/'media-compat-jobs'; root.mkdir(parents=True, exist_ok=True)
    def call(suffix, value):
        return pipeline.api_call(backend, token, 'POST', '/api/internal/capture/media-compat/'+suffix,
            {'workerId': worker_id, **value}, retry_transient=True)
    def deliver(path, saved):
        try:
            ack = call(saved['jobNo']+'/complete', saved['receipt'])
        except pipeline.BackendError as error:
            if error.http_status != 409: raise
            # A rejected verification receipt is a terminal processing failure,
            # not a journal that should block every later job forever.
            saved['receipt'] = dict(executionVersion=saved['receipt']['executionVersion'],
                status='FAILED', errorCode='RESULT_VALIDATION_FAILED', retryable=False)
            storage.write_journal(path, saved)
            ack = call(saved['jobNo']+'/complete', saved['receipt'])
        path.unlink()
        if saved['receipt'].get('status') == 'SUCCEEDED' and ack.get('backupVerified'):
            cleanup_cache(saved['receipt'])
    # One shared Worker job at a time across all tenant processes. The encoder
    # has a separate global lock also shared with new-video ingress.
    lock_path = Path(os.environ.get('HM_MEDIA_JOB_LOCK', '/opt/data/media-compat-job.lock'))
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open('a') as lock:
        try: fcntl.flock(lock, fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError: return False
        for path in sorted(root.glob('*.json')):
            saved = json.loads(path.read_text())
            if saved['workerId'] != worker_id: continue
            deliver(path, saved)
        priority_only = False
        registry_path = os.environ.get('HM_TENANT_CONFIG')
        if registry_path:
            registry = json.loads(Path(registry_path).read_text())
            for other in registry.values():
                if other.get('workerId') == worker_id: continue
                try:
                    pending = pipeline.api_call(other['backendUrl'], other['workerToken'], 'POST',
                        '/api/internal/capture/media-compat/pending', {'workerId': other['workerId']}, retry_transient=False)
                    if pending.get('urgent', 0): priority_only = True
                except Exception:
                    # During rolling upgrades, never let history compete with an
                    # unknown foreground backlog. Local foreground still runs.
                    priority_only = True
        job = call('claim', {'priorityOnly': priority_only})
        if not job: return False
        receipt = {'executionVersion': job['executionVersion'], 'status': 'SUCCEEDED'}
        try:
            with storage.lease(lambda: call(job['jobNo']+'/check', {'executionVersion': job['executionVersion']})) as guard:
                receipt.update(execute(job, args, pipeline, guard))
                guard()
        except Exception as exc:
            code = str(exc) if isinstance(exc, (JobFailure, media.CompatibilityError, storage.StorageFailure)) else 'MEDIA_COMPAT_IO_FAILED'
            retryable = getattr(exc, 'retryable', not isinstance(exc, (JobFailure, media.CompatibilityError)))
            receipt.update(status='FAILED', errorCode=code, retryable=retryable)
        path = root/(job['jobNo']+'-a'+str(job['executionVersion'])+'.json')
        storage.write_journal(path, {'jobNo': job['jobNo'], 'workerId': worker_id, 'receipt': receipt})
        deliver(path, {'jobNo': job['jobNo'], 'workerId': worker_id, 'receipt': receipt})
        return True


def cleanup_cache(receipt):
    # Only after the backend durably acknowledges the independently verified,
    # encrypted bucket backup. Keep JSON receipts; never touch capture roots.
    if not receipt.get('backupKey') or not receipt.get('backupETag'): return
    base = Path(receipt['backupPath']).parent.resolve()
    root = Path(os.environ.get('HM_MEDIA_BACKUP_ROOT', '/opt/data/media-compat-backups')).resolve()
    if root not in base.parents or not base.name.startswith('M-'): return
    for path in base.rglob('*'):
        if path.is_file() and path.suffix in ('.mp4', '.part', '.webm', '.mkv'):
            path.unlink()
