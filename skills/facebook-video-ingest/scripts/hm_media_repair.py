"""Privileged maintenance helper. JSON stdin/stdout; no credentials leave this process."""
from __future__ import annotations
import hashlib
import json
import os
import pwd
import re
from pathlib import Path
import sys
from urllib.parse import quote
import hm_media_compat as media
import hm_review_storage as storage


def process(job):
    region = job['region'].upper()
    if region not in {'PH', 'TH', 'VN', 'ID'}: raise ValueError('Invalid region')
    config = json.loads(Path(os.environ.get('HM_TENANT_CONFIG', '/opt/hm/tenants.json')).read_text())[region.lower()]
    os.environ['HM_R2_KEY_PREFIX'] = region
    os.environ['HM_REVIEW_KEY_FILE'] = config['reviewKeyFile']
    if not re.fullmatch(r'V-[A-Za-z0-9-]+', job['videoNo']): raise ValueError('Invalid video number')
    key, version = job['key'], job.get('keyVersion')
    review = key.startswith('review/' + region + '/')
    if not review and not key.startswith(region + '/'): raise ValueError('Region mismatch')
    extra = storage.encryption(storage.configuration(region), version) if review else {}
    client, bucket = storage.client(), os.environ['CLOUDFLARE_R2_BUCKET']
    before = client.head_object(Bucket=bucket, Key=key, **extra)
    if os.getuid() == 0:
        user=pwd.getpwnam('hermes'); os.setgid(user.pw_gid); os.setuid(user.pw_uid)
    base = Path('/opt/data/media-compat-backups') / region / job['videoNo']
    base.mkdir(mode=0o700, parents=True, exist_ok=True)
    source = base / (hashlib.sha256((key + before['ETag']).encode()).hexdigest() + '.original.mp4')
    if not source.exists():
        tmp = source.with_suffix('.part')
        try:
            response = client.get_object(Bucket=bucket, Key=key, IfMatch=before['ETag'], **extra)
            with tmp.open('wb') as out:
                for chunk in response['Body'].iter_chunks(chunk_size=1024 * 1024): out.write(chunk)
            tmp.replace(source)
        finally: tmp.unlink(missing_ok=True)
    digest = media.sha256(source)
    if source.stat().st_size != job['fileSize'] or (job.get('sha256') and digest != job['sha256']):
        raise ValueError('Source hash/size changed')
    info = media.probe(source)
    result = dict(compatible=media.compatible(source, info), sourceKey=key, sourceETag=before['ETag'],
                  sourceSha256=digest, backupPath=str(source), codecs=[s.get('codec_name') for s in media.streams(info)])
    if job.get('action') != 'prepare' or result['compatible']: return result
    normalized = media.normalize(source)
    target = key.rsplit('/', 1)[0] + '/compat-' + job['videoNo'] + '-' + digest[:20] + '.mp4'
    metadata = dict(before.get('Metadata', {}), **{'hm-sha256': normalized['sha256'], 'hm-compat-version': str(media.VERSION)})
    existing = storage.head(client, bucket, target, extra)
    if not existing:
        with Path(normalized['path']).open('rb') as body:
            client.put_object(Bucket=bucket, Key=target, Body=body, ContentType='video/mp4',
                              Metadata=metadata, IfNoneMatch='*', **extra)
    after = client.head_object(Bucket=bucket, Key=target, **extra)
    if after['ContentLength'] != normalized['fileSize'] or after.get('Metadata', {}).get('hm-sha256') != normalized['sha256']:
        raise ValueError('Target HEAD mismatch')
    verified = hashlib.sha256()
    response = client.get_object(Bucket=bucket, Key=target, IfMatch=after['ETag'], **extra)
    for chunk in response['Body'].iter_chunks(chunk_size=1024 * 1024): verified.update(chunk)
    if verified.hexdigest() != normalized['sha256']: raise ValueError('Target download hash mismatch')
    if client.head_object(Bucket=bucket, Key=key, **extra)['ETag'] != before['ETag']:
        raise ValueError('Source object changed during conversion')
    result.update(normalized, targetKey=target, targetETag=after['ETag'], keyVersion=version,
                  targetUrl=os.environ.get('CLOUDFLARE_R2_PUBLIC_BASE_URL', '').rstrip('/') + '/' + quote(target, safe='/'))
    return result


if __name__ == '__main__':
    os.umask(0o077)
    try:
        print(json.dumps(process(json.load(sys.stdin)), ensure_ascii=False))
    except Exception as exc:
        # Keep credentials / SDK request headers out of maintenance logs.
        print(json.dumps({'error': type(exc).__name__, 'message': str(exc) if isinstance(exc, (ValueError, media.CompatibilityError)) else 'Media maintenance failed'}))
        sys.exit(1)
