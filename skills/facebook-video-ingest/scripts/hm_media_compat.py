"""Non-destructive, content-addressed iPhone media normalization shared by all ingress paths."""
from __future__ import annotations
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import struct
import subprocess
import tempfile

VERSION = 1


class CompatibilityError(RuntimeError):
    pass


def sha256(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def run(argv):
    try:
        return subprocess.run(argv, check=True, capture_output=True, timeout=7200).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        # Do not leak input URLs, paths or ffmpeg stderr into public worker messages.
        raise CompatibilityError('MEDIA_COMPAT_PROCESS_FAILED') from exc


def probe(path):
    try:
        result = json.loads(run([os.environ.get('FFPROBE', 'ffprobe'), '-v', 'error',
            '-show_format', '-show_streams', '-of', 'json', str(path)]))
        video = next(s for s in result['streams'] if s['codec_type'] == 'video' and not s.get('disposition', {}).get('attached_pic'))
        duration = float(result['format']['duration'])
        if not math.isfinite(duration) or duration <= 0 or not video.get('width') or not video.get('height'):
            raise ValueError()
        return result
    except (KeyError, ValueError, StopIteration) as exc:
        raise CompatibilityError('MEDIA_COMPAT_INVALID_MEDIA') from exc


def streams(info):
    return [s for s in info['streams'] if s.get('codec_type') in ('video', 'audio') and not s.get('disposition', {}).get('attached_pic')]


def faststart(path):
    """Read MP4 atom headers, without loading video payloads into memory."""
    size = Path(path).stat().st_size
    with Path(path).open('rb') as f:
        while f.tell() + 8 <= size:
            start = f.tell()
            length, kind = struct.unpack('>I4s', f.read(8))
            if length == 1:
                data = f.read(8)
                if len(data) != 8: return False
                length = struct.unpack('>Q', data)[0]
            if kind == b'moov': return True
            if kind == b'mdat' or length < 8 or start + length > size: return False
            f.seek(start + length)
    return False


def compatible_video(info):
    media = streams(info)
    videos = [s for s in media if s['codec_type'] == 'video']
    if len(videos) != 1: return False
    v = videos[0]
    rate = v.get('avg_frame_rate', '0/1').split('/')
    fps = float(rate[0]) / max(float(rate[-1]), 1)
    return (fps <= 30.01
        and max(v['width'], v['height']) <= 1920 and min(v['width'], v['height']) <= 1080
        and videos[0].get('codec_name') == 'h264' and videos[0].get('codec_tag_string') == 'avc1'
        and videos[0].get('pix_fmt') == 'yuv420p' and videos[0].get('profile') in ('Constrained Baseline', 'Baseline', 'Main', 'High')
        and int(videos[0].get('level', 999)) <= 42)


def compatible_audio(info):
    audio = [s for s in streams(info) if s['codec_type'] == 'audio']
    return len(audio) <= 1 and all(s.get('codec_name') == 'aac' and s.get('profile') == 'LC'
                                 and int(s.get('channels', 99)) <= 2 for s in audio)


def compatible(path, info):
    return (info['format'].get('tags', {}).get('major_brand', '').strip() != 'qt'
        and 'mp4' in info['format'].get('format_name', '').split(',')
        and compatible_video(info) and compatible_audio(info)
        and faststart(path))


def decode(path):
    run([os.environ.get('FFMPEG', 'ffmpeg'), '-nostdin', '-v', 'error', '-xerror', '-err_detect', 'explode',
         '-threads', '2', '-i', str(path), '-map', '0:v:0', '-map', '0:a:0?', '-f', 'null', '-'])


def validate(source, target):
    before, after = float(source['format']['duration']), float(target['format']['duration'])
    if abs(before - after) > max(.25, before * .005):
        raise CompatibilityError('MEDIA_COMPAT_DURATION_MISMATCH')
    a, b = streams(source), streams(target)
    if sum(s['codec_type'] == 'audio' for s in a) > 0 and not any(s['codec_type'] == 'audio' for s in b):
        raise CompatibilityError('MEDIA_COMPAT_AUDIO_MISSING')
    # Preserve measured audio/video offsets, including legitimate source offsets.
    def timing(items):
        v = next(s for s in items if s['codec_type'] == 'video')
        audio = next((s for s in items if s['codec_type'] == 'audio'), None)
        if not audio: return None
        return (float(audio.get('start_time', 0)) - float(v.get('start_time', 0)),
                float(audio.get('duration', before)) - float(v.get('duration', before)))
    x, y = timing(a), timing(b)
    if x and y and any(abs(p - q) > .25 for p, q in zip(x, y)):
        raise CompatibilityError('MEDIA_COMPAT_AV_SYNC_MISMATCH')


def normalize(source):
    # All tenants and ingress paths share the same encoder budget.
    import hm_media_capacity
    with hm_media_capacity.encoder_slot():
        return _normalize(source)


def _normalize(source):
    source = Path(source).resolve(strict=True)
    source_hash = sha256(source)
    cache = source.parent / '.hm-compatible'
    cache.mkdir(mode=0o700, exist_ok=True)
    key = f'v{VERSION}-{source_hash}'
    output, receipt = cache / (key + '.mp4'), cache / (key + '.json')
    with (cache / (key + '.lock')).open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if receipt.exists():
            saved = json.loads(receipt.read_text())
            candidate = Path(saved['path'])
            if candidate.is_file() and sha256(candidate) == saved['sha256']:
                return saved
        original = probe(source)
        if compatible(source, original):
            decode(source)
            result = dict(path=str(source), sha256=source_hash, fileSize=source.stat().st_size,
                          durationSeconds=float(original['format']['duration']), converted=False)
        else:
            fd, name = tempfile.mkstemp(prefix=key + '-', suffix='.mp4', dir=cache)
            os.close(fd)
            temporary = Path(name)
            try:
                video = next(s for s in streams(original) if s['codec_type'] == 'video')
                # FFmpeg autorotates before filters. Square pixels retain anamorphic display ratio.
                filters = "scale=w='trunc(iw*sar/2)*2':h=ih,setsar=1,scale=w='min(iw,if(gte(iw,ih),1920,1080))':h='min(ih,if(gte(iw,ih),1080,1920))':force_original_aspect_ratio=decrease:force_divisible_by=2"
                rate = video.get('avg_frame_rate', '0/1').split('/')
                fps = float(rate[0]) / max(float(rate[-1]), 1)
                if fps > 30: filters += ',fps=30'
                # An incompatible audio profile or MP4 index does not require
                # re-encoding already compatible video. Preserve its packets,
                # rotation and quality; still verify the entire output below.
                copy_video, copy_audio = compatible_video(original), compatible_audio(original)
                video_args = ['-c:v', 'copy'] if copy_video else [
                    '-vf', filters, '-c:v', 'libx264', '-threads', '2', '-filter_threads', '1',
                    '-preset', 'medium', '-crf', '20', '-pix_fmt', 'yuv420p', '-profile:v', 'high']
                audio_args = ['-c:a', 'copy'] if copy_audio else [
                    '-c:a', 'aac', '-profile:a', 'aac_low', '-b:a', '160k', '-ac', '2', '-threads:a', '2']
                run([os.environ.get('FFMPEG', 'ffmpeg'), '-nostdin', '-v', 'error', '-xerror', '-y',
                     '-threads', '2', '-i', str(source), '-map', '0:v:0', '-map', '0:a:0?', '-sn', '-dn',
                     *video_args, '-tag:v', 'avc1', *audio_args,
                     '-movflags', '+faststart', str(temporary)])
                info = probe(temporary)
                if not compatible(temporary, info): raise CompatibilityError('MEDIA_COMPAT_OUTPUT_UNSUPPORTED')
                validate(original, info)
                decode(temporary)
                if sha256(source) != source_hash: raise CompatibilityError('MEDIA_COMPAT_SOURCE_CHANGED')
                result = dict(path=str(output), sha256=sha256(temporary), fileSize=temporary.stat().st_size,
                              durationSeconds=float(info['format']['duration']), converted=True,
                              videoReencoded=not copy_video, audioReencoded=not copy_audio)
                temporary.replace(output)
            finally:
                temporary.unlink(missing_ok=True)
        if sha256(source) != source_hash: raise CompatibilityError('MEDIA_COMPAT_SOURCE_CHANGED')
        result.update(sourcePath=str(source), sourceSha256=source_hash, version=VERSION)
        temp_receipt = receipt.with_suffix('.tmp')
        temp_receipt.write_text(json.dumps(result))
        temp_receipt.replace(receipt)
        return result


def prepare_record(video):
    if os.environ.get('HM_MEDIA_COMPAT_ENABLED', '0') != '1': return
    try:
        original_name = Path(video.get('fileName') or video['localPath']).name
        if Path(original_name).suffix.lower() in ('.mp4', '.webm', '.mkv', '.mov', '.m4v', '.avi'):
            original_name = Path(original_name).stem
        result = normalize(video['localPath'])
        video.update(localPath=result['path'], fileName=original_name + '.mp4',
                     fileSize=result['fileSize'], sha256=result['sha256'], durationSeconds=round(result['durationSeconds']),
                     mediaCompatibility=result)
    except (CompatibilityError, OSError, ValueError, KeyError) as exc:
        video.update(status='download-failed', errorCode='MEDIA_COMPAT_FAILED',
                     error='视频兼容处理失败，原文件已保留，请重试。')
        if video.get('statistics'): video['statistics']['outcome'] = 'FAILURE'
