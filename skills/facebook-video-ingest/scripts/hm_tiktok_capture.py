"""Anonymous TikTok provider; reuse the independent skill's verified archive."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import shutil
import sys

SCRIPTS = Path(__file__).resolve().parents[2] / 'tiktok-video-downloader/scripts'
spec = importlib.util.spec_from_file_location('hm_tiktok_download', SCRIPTS / 'download.py')
downloader = importlib.util.module_from_spec(spec)
spec.loader.exec_module(downloader)
CLIENT = 'chrome-131:macos-14'


class Entries(list):
    pass


def provider():
    # Browser fallback imports its sibling without replacing YouTube's download module.
    if str(SCRIPTS) not in sys.path: sys.path.append(str(SCRIPTS))
    return downloader.Extractor(downloader.ffmpeg_binary(),
        Path(os.environ.get('TMPDIR', '/tmp')) / 'tiktok-anonymous', CLIENT)


def source(url):
    return downloader.normalize_url(url)


def discover(url, limit):
    engine = provider()
    parent = source(url)
    result = Entries(downloader.entry_source(item, parent) for item in engine.discover(parent, limit))
    result.discovery = engine.discovery
    if not result: raise downloader.Failure('DISCOVERY_EMPTY')
    return result


def download(item, output):
    engine = provider()
    current = source(item['url'])
    reason = downloader.unsupported(current)
    if reason: return dict(item, status='unsupported', errorCode=reason)
    raw = output / 'originals'
    raw.mkdir(parents=True, exist_ok=True)
    archive_file = raw / '.download-archive.json'
    archive = json.loads(archive_file.read_text()) if archive_file.exists() else {}
    if current['id'] not in archive and not (raw / '.pending' / (current['id']+'.json')).exists():
        info = engine.inspect(current)
        reason = downloader.unsupported(current, info)
        if reason: return dict(item, status='unsupported', errorCode=reason)
        if (info.get('duration') or 0) > 1200:
            return dict(item, status='filtered-duration', duration=info['duration'])
    saved = downloader.download_one(current, raw, archive, engine, engine.ffmpeg)
    original = raw / saved['path']
    preview = output / 'previews' / (current['id']+'.mp4')
    preview.parent.mkdir(parents=True, exist_ok=True)
    if saved['videoCodec'] == 'h264' and saved.get('audioCodec') in (None, 'aac'):
        # 审核暂存会清理预览路径，必须与长期保留的原文件分开。
        if not preview.is_file() or downloader.digest(preview) != saved['sha256']:
            temporary = preview.with_suffix('.partial.mp4')
            shutil.copy2(original, temporary)
            temporary.replace(preview)
        verified = saved
    else:
        if not preview.exists():
            temporary = preview.with_suffix('.partial.mp4')
            proc = subprocess.run([engine.ffmpeg, '-nostdin', '-v', 'error', '-y', '-i', str(original),
                '-map', '0:v:0', '-map', '0:a:0?', '-c:v', 'libx264', '-preset', 'fast', '-crf', '18',
                '-threads', '2', '-vf', 'pad=ceil(iw/2)*2:ceil(ih/2)*2', '-pix_fmt', 'yuv420p',
                '-c:a', 'aac', '-b:a', '192k', '-movflags', '+faststart', str(temporary)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3600)
            if proc.returncode: raise downloader.Failure('VALIDATION_FAILED')
            downloader.validate_file(temporary, engine.ffmpeg, bool(saved.get('audioCodec')))
            temporary.replace(preview)
        try: verified = downloader.validate_file(preview, engine.ffmpeg, bool(saved.get('audioCodec')))
        except downloader.Failure:
            preview.replace(preview.with_suffix('.invalid.mp4'))
            raise
    filename = original.name
    return dict(item, status=saved.get('status','downloaded'), title=saved.get('title') or current['id'],
        upload_date=filename[:8] if filename[:8].isdigit() else None,
        duration=saved['duration'], path=str(preview), bytes=verified['bytes'],
        sha256=verified['sha256'], decodePassed=True, originalPath=str(original),
        originalSha256=saved['sha256'], requestClient=CLIENT)


def receipt_valid(item):
    for path_key, hash_key in (('path','sha256'),('originalPath','originalSha256')):
        path=Path(item.get(path_key) or '')
        if not path.is_file() or downloader.digest(path)!=item.get(hash_key): return False
    return True
