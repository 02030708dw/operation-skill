#!/usr/bin/env python3
"""Anonymous, bounded TikTok downloads. No browser credentials or yt-dlp config."""
import argparse
import datetime as dt
import fcntl
import hashlib
import itertools
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import time
import uuid
from urllib.parse import urlparse

MESSAGES = {
    'LOGIN_REQUIRED': '该内容明确要求登录；匿名模式不读取账号会话。',
    'VERIFICATION_REQUIRED': '平台要求安全验证，已停止本批次。',
    'RATE_LIMITED': '平台限流，已停止本批次；不会切换账号或代理重试。',
    'ACCESS_DENIED': '平台拒绝访问或内容不可用，不能仅据此断定需要登录。',
    'NETWORK_ERROR': '网络请求失败，最多补试一次后结束。',
    'DISCOVERY_EMPTY': '未能从主页枚举视频，数量未知。',
    'EXTRACTION_ERROR': '无法解析平台返回内容，不等同于需要登录。',
    'UNSUPPORTED_PHOTO': '图文内容不在本次视频下载范围内。',
    'UNSUPPORTED_LIVE': '直播不在本次视频下载范围内。',
    'UNSUPPORTED_CONTENT': '没有可下载的视频流。',
    'VALIDATION_FAILED': '文件音视频校验未通过，不写入成功归档。',
    'INTERRUPTED': '执行被中断，分片已保留，可重新运行续传。',
    'BUSY': '输出目录已有任务运行，请稍后重试。',
    'ENVIRONMENT_ERROR': '运行环境或持久化目录不可用。',
}
STOP = {'RATE_LIMITED', 'VERIFICATION_REQUIRED'}
AUTH_ENV = {'HM_GOOGLE_COOKIES', 'HM_FACEBOOK_PROFILE', 'HM_FACEBOOK_ACCOUNT_STATE',
            'FACEBOOK_FOLLOWED_COOKIES', 'FB_FOLLOWED_COOKIES', 'YTDLP_COOKIES',
            'YTDLP_COOKIES_FROM_BROWSER', 'TIKTOK_COOKIES', 'TIKTOK_PROFILE'}

class Failure(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(MESSAGES[code])

class QuietLogger:
    def debug(self, message): pass
    def warning(self, message): pass
    def error(self, message): pass


def normalize_url(value):
    p = urlparse(value)
    if p.scheme != 'https' or p.hostname not in {'www.tiktok.com', 'tiktok.com', 'm.tiktok.com'} or p.username or p.password or p.port:
        raise ValueError('需要 TikTok 官方 HTTPS 主页或单条链接')
    match = re.fullmatch(r'/@([A-Za-z0-9_.-]+)(?:/(video|photo)/([0-9]+)|/(live))?/?', p.path)
    if not match:
        raise ValueError('支持 /@账号、/@账号/video/ID；图文和直播会单独标记为不支持')
    user, kind, vid, live = match.groups()
    kind = 'live' if live else kind or 'profile'
    suffix = '/live' if live else '/' + kind + '/' + vid if vid else ''
    return {'url': 'https://www.tiktok.com/@' + user + suffix, 'account': user, 'id': vid, 'kind': kind}


def classify(error):
    if isinstance(error, Failure): return error.code
    text = str(error).lower()
    if any(x in text for x in ('429', 'too many requests', 'rate limit')): return 'RATE_LIMITED'
    if any(x in text for x in ('captcha', 'verify your', 'security check', 'verification required', 'challenge', 'not a bot')): return 'VERIFICATION_REQUIRED'
    if any(x in text for x in ('log in to', 'login required', 'login to', 'sign in to', 'authentication required', 'only available for registered')): return 'LOGIN_REQUIRED'
    if any(x in text for x in ('private', '403', '404', 'access denied', 'not available', 'unavailable', 'permission', 'geo-restrict', 'not authorized')): return 'ACCESS_DENIED'
    if any(x in text for x in ('timed out', 'timeout', 'connection', 'network', 'resolve host', '502', '503', '504')): return 'NETWORK_ERROR'
    return 'EXTRACTION_ERROR'


def network_call(fn):
    for attempt in range(2):
        try: return fn()
        except Exception as error:
            code = classify(error)
            if code != 'NETWORK_ERROR' or attempt == 1: raise Failure(code) from None
            time.sleep(1)


def save(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.tmp')
    with temp.open('w', encoding='utf-8') as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2)
        stream.flush(); os.fsync(stream.fileno())
    temp.replace(path)
    fd = os.open(path.parent, os.O_RDONLY)
    try: os.fsync(fd)
    finally: os.close(fd)


def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''): h.update(block)
    return h.hexdigest()


def component(value, limit=120):
    text = re.sub(r'[\x00-\x1f\x7f/\\:*?"<>|]', '_', str(value or 'untitled')).strip(' .')
    return (text.encode('utf-8')[:limit].decode('utf-8', 'ignore').rstrip(' .') or 'untitled')


def media_path(output, source, info):
    date = info.get('upload_date') or 'unknown-date'
    if not re.fullmatch(r'\d{8}', date): date = 'unknown-date'
    return output / component(source['account'], 80) / f"{date}_{component(info.get('title'))}_[{source['id']}].mp4"


def validate_file(path, ffmpeg, expect_audio=True):
    if not path.is_file() or path.stat().st_size <= 0: raise Failure('VALIDATION_FAILED')
    try:
        probe = subprocess.run([ffmpeg, '-hide_banner', '-i', str(path)], capture_output=True, text=True, timeout=60)
        video = re.search(r'Stream #[^\n]*Video: ([^ ,]+)[^\n]*? (\d{2,5})x(\d{2,5})', probe.stderr)
        audio = re.search(r'Stream #[^\n]*Audio: ([^ ,]+)', probe.stderr)
        duration = re.search(r'Duration: (\d+):(\d+):([\d.]+)', probe.stderr)
        if not video or not duration or (expect_audio and not audio): raise Failure('VALIDATION_FAILED')
        seconds = int(duration[1]) * 3600 + int(duration[2]) * 60 + float(duration[3])
        if seconds <= 0: raise Failure('VALIDATION_FAILED')
        decoded = subprocess.run([ffmpeg, '-nostdin', '-v', 'error', '-xerror', '-i', str(path),
                                  '-map', '0:v:0', '-map', '0:a:0?', '-f', 'null', '-'],
                                 capture_output=True, timeout=max(120, min(3600, seconds * 3)))
        if decoded.returncode: raise Failure('VALIDATION_FAILED')
        return {'bytes': path.stat().st_size, 'sha256': digest(path), 'duration': seconds,
                'width': int(video[2]), 'height': int(video[3]), 'videoCodec': video[1],
                'audioCodec': audio[1] if audio else None, 'decodePassed': True}
    except (OSError, subprocess.TimeoutExpired): raise Failure('VALIDATION_FAILED') from None


def ffmpeg_binary():
    if shutil.which('ffmpeg'): return shutil.which('ffmpeg')
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except (ImportError, RuntimeError): raise Failure('ENVIRONMENT_ERROR') from None


class Extractor:
    def __init__(self, ffmpeg): self.ffmpeg = ffmpeg
    def options(self):
        return {'logger': QuietLogger(), 'quiet': True, 'no_warnings': True, 'noprogress': True,
                'cachedir': False, 'cookiefile': None, 'cookiesfrombrowser': None, 'usenetrc': False,
                'socket_timeout': 15, 'retries': 0, 'extractor_retries': 0, 'fragment_retries': 0,
                'ignoreerrors': False, 'ffmpeg_location': self.ffmpeg}
    def discover(self, source, limit):
        import yt_dlp
        def request():
            with yt_dlp.YoutubeDL(dict(self.options(), extract_flat='in_playlist', skip_download=True,
                                      playlistend=limit, lazy_playlist=True)) as ydl:
                info = ydl.extract_info(source['url'], download=False)
                return list(itertools.islice(info.get('entries') or [], limit)) if info else []
        return network_call(request)
    def inspect(self, source):
        import yt_dlp
        def request():
            with yt_dlp.YoutubeDL(dict(self.options(), noplaylist=True, skip_download=True)) as ydl:
                return ydl.extract_info(source['url'], download=False)
        return network_call(request)
    def download(self, source, target):
        import yt_dlp
        def request():
            with yt_dlp.YoutubeDL(dict(self.options(), noplaylist=True, continuedl=True, overwrites=False,
                                      format='bv*+ba/b', merge_output_format='mp4',
                                      outtmpl=str(target).replace('%', '%%'), windowsfilenames=False)) as ydl:
                ydl.extract_info(source['url'], download=True)
        network_call(request)


def unsupported(source, info=None):
    if source['kind'] == 'photo': return 'UNSUPPORTED_PHOTO'
    if source['kind'] == 'live' or info and (info.get('is_live') or info.get('live_status') in {'is_live', 'is_upcoming'}): return 'UNSUPPORTED_LIVE'
    if info and info.get('_type') in {'playlist', 'multi_video'}: return 'UNSUPPORTED_PHOTO'
    if info is not None and not info.get('formats'): return 'UNSUPPORTED_CONTENT'
    return None


def entry_source(entry, parent):
    if not isinstance(entry, dict): raise Failure('EXTRACTION_ERROR')
    url = entry.get('webpage_url') or entry.get('url')
    if url and url.startswith('https://'):
        try:
            source = normalize_url(url)
            if source['kind'] != 'profile' and source['account'].lower() == parent['account'].lower(): return source
        except ValueError: pass
    vid = str(entry.get('id', ''))
    if not vid.isdigit(): raise Failure('EXTRACTION_ERROR')
    return normalize_url(parent['url'] + '/video/' + vid)


def download_one(source, output, archive, extractor, ffmpeg, validator=validate_file):
    vid = source['id']; reason = unsupported(source)
    if reason: raise Failure(reason)
    receipt = archive.get(vid)
    if receipt:
        path = (output / receipt['path']).resolve()
        if path.is_relative_to(output) and path.is_file() and digest(path) == receipt['sha256']:
            return dict(receipt, status='skipped', reasonCode='ALREADY_DOWNLOADED')
    pending = output / '.pending' / (vid + '.json')
    if pending.exists():
        meta = json.loads(pending.read_text(encoding='utf-8'))
        target = (output / meta['path']).resolve()
        if not target.is_relative_to(output): raise Failure('ENVIRONMENT_ERROR')
    else:
        info = extractor.inspect(source)
        reason = unsupported(source, info)
        if not info: raise Failure('EXTRACTION_ERROR')
        if reason: raise Failure(reason)
        if str(info.get('id')) != vid: raise Failure('EXTRACTION_ERROR')
        target = media_path(output, source, info)
        meta = {'id': vid, 'url': source['url'], 'title': str(info.get('title') or '')[:500],
                'path': str(target.relative_to(output)), 'expectAudio': info.get('acodec') != 'none'}
        save(pending, meta)
    target.parent.mkdir(parents=True, exist_ok=True)
    recovered = target.is_file()
    if not recovered: extractor.download(source, target)
    try: verified = validator(target, ffmpeg, meta['expectAudio'])
    except Failure:
        # A completed but invalid file must not permanently prevent another attempt.
        if target.is_file(): target.replace(target.with_name(target.name + '.invalid-' + uuid.uuid4().hex[:8]))
        raise
    result = {**meta, **verified, 'status': 'downloaded', 'recovered': recovered}
    archive[vid] = result
    save(output / '.download-archive.json', archive)
    pending.unlink(missing_ok=True)
    return result


def finish(report):
    rows = report['results']
    report['counts'] = {key: sum(x['status'] == key for x in rows) for key in ('downloaded', 'skipped', 'login-required', 'failed', 'unsupported', 'listed')}
    report['counts']['unattempted'] = None if report['discovered'] is None else report['discovered'] - len(rows)
    error = report.get('error', {}).get('code')
    if error: report['status'] = 'LOGIN_REQUIRED' if error == 'LOGIN_REQUIRED' else 'STOPPED' if error in STOP or error == 'INTERRUPTED' else 'FAILED'
    elif report['counts']['failed']: report['status'] = 'PARTIAL' if any(x['status'] in {'downloaded', 'skipped'} for x in rows) else 'FAILED'
    elif report['counts']['login-required']: report['status'] = 'PARTIAL' if any(x['status'] in {'downloaded', 'skipped'} for x in rows) else 'LOGIN_REQUIRED'
    elif report['counts']['unsupported'] and report['counts']['unsupported'] == len(rows): report['status'] = 'UNSUPPORTED'
    else: report['status'] = 'LISTED' if report['mode'] == 'list' else 'COMPLETED'
    report['finishedAt'] = dt.datetime.now(dt.timezone.utc).isoformat()
    return report


def run(source, output, limit, list_only, extractor, ffmpeg, report_path, validator=validate_file):
    report = {'source': source['url'], 'mode': 'list' if list_only else 'download', 'authentication': 'ANONYMOUS',
              'limit': 1 if source['kind'] != 'profile' else limit, 'startedAt': dt.datetime.now(dt.timezone.utc).isoformat(),
              'discovered': None, 'results': [], 'discovery': {'status': 'NOT_NEEDED'}}
    archive_path = output / '.download-archive.json'
    archive = json.loads(archive_path.read_text(encoding='utf-8')) if archive_path.exists() else {}
    try:
        if source['kind'] == 'profile':
            report['discovery'] = {'status': 'RUNNING'}
            entries = extractor.discover(source, limit)
            if not entries: raise Failure('DISCOVERY_EMPTY')
            entries = entries[:limit]
            report['discovery'] = {'status': 'COMPLETED'}
        else: entries = [source]
        report['discovered'] = len(entries)
        save(report_path, report)
        for entry in entries:
            current = None
            try:
                current = entry_source(entry, source) if source['kind'] == 'profile' else source
                if list_only:
                    code = unsupported(current)
                    if code: raise Failure(code)
                    result = {'id': current['id'], 'url': current['url'], 'status': 'listed'}
                else:
                    result = download_one(current, output, archive, extractor, ffmpeg, validator)
                report['results'].append(result)
            except Exception as error:
                code = classify(error)
                status = 'login-required' if code == 'LOGIN_REQUIRED' else 'unsupported' if code.startswith('UNSUPPORTED_') else 'failed'
                report['results'].append({'id': current['id'] if current else None, 'url': current['url'] if current else None,
                                          'status': status, 'reasonCode': code, 'reason': MESSAGES[code]})
                if code in STOP: raise Failure(code) from None
            save(report_path, finish(report))
    except KeyboardInterrupt:
        report['error'] = {'code': 'INTERRUPTED', 'reason': MESSAGES['INTERRUPTED']}
    except Exception as error:
        code = classify(error); report['error'] = {'code': code, 'reason': MESSAGES[code]}
        if report['discovered'] is None: report['discovery'] = {'status': 'FAILED', 'reasonCode': code}
    save(report_path, finish(report))
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('url')
    parser.add_argument('--limit', type=int, default=10)
    parser.add_argument('--list-only', action='store_true')
    parser.add_argument('--output', type=Path, default=Path.home() / 'Downloads/TikTok')
    args = parser.parse_args()
    if not 1 <= args.limit <= 1000: parser.error('--limit 必须在 1 到 1000 之间')
    try: source = normalize_url(args.url)
    except ValueError as error: parser.error(str(error))
    for key in AUTH_ENV: os.environ.pop(key, None)
    output = args.output.expanduser().resolve(); output.mkdir(parents=True, exist_ok=True)
    def stop(signum, frame): raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, stop)
    with (output / '.download.lock').open('a') as lock:
        try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(json.dumps({'status': 'BUSY', 'reason': MESSAGES['BUSY']}, ensure_ascii=False)); return 2
        stamp = dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S') + '-' + uuid.uuid4().hex[:8]
        report_path = output / '.reports' / (stamp + '.json')
        try:
            ffmpeg = ffmpeg_binary()
            report = run(source, output, args.limit, args.list_only, Extractor(ffmpeg), ffmpeg, report_path)
        except (OSError, ValueError, Failure):
            print(json.dumps({'status': 'ENVIRONMENT_ERROR', 'reason': MESSAGES['ENVIRONMENT_ERROR']}, ensure_ascii=False)); return 2
        print(json.dumps({'status': report['status'], 'report': str(report_path), 'counts': report['counts'], 'error': report.get('error')}, ensure_ascii=False))
        return 130 if report.get('error', {}).get('code') == 'INTERRUPTED' else 0 if report['status'] in {'COMPLETED', 'LISTED'} else 1

if __name__ == '__main__': sys.exit(main())
