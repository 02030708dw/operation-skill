import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('download', Path(__file__).parents[1] / 'scripts/download.py')
d = importlib.util.module_from_spec(spec)
spec.loader.exec_module(d)
PROFILE = 'https://www.tiktok.com/@example'

class FakeExtractor:
    def __init__(self, ids=('1', '2', '3'), errors=None):
        self.ids = ids
        self.errors = errors or {}
        self.calls = []
        self.resumed = False
    def discover(self, source, limit):
        return [{'id': vid} for vid in self.ids]
    def inspect(self, source):
        self.calls.append(('inspect', source['id']))
        error = self.errors.get(source['id'])
        if error: raise RuntimeError(error)
        return {'id': source['id'], 'title': '100% hello / 🐱', 'upload_date': '20260918', 'formats': [{}], 'acodec': 'aac'}
    def download(self, source, target):
        self.calls.append(('download', source['id']))
        part = target.with_suffix('.mp4.part')
        self.resumed = part.exists()
        target.write_bytes((part.read_bytes() if part.exists() else b'') + b'media')
        part.unlink(missing_ok=True)


def validated(path, ffmpeg, audio=True):
    return {'sha256': d.digest(path), 'bytes': path.stat().st_size, 'decodePassed': True}

class DownloadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.output = Path(self.temp.name).resolve()
    def run_case(self, extractor=None, url=PROFILE, limit=10, list_only=False, validator=validated):
        return d.run(d.normalize_url(url), self.output, limit, list_only, extractor or FakeExtractor(), 'ffmpeg', self.output / 'report.json', validator)
    def test_urls_and_no_query_credentials(self):
        self.assertEqual(d.normalize_url(PROFILE + '/video/123?token=secret')['url'], PROFILE + '/video/123')
        for bad in ('http://www.tiktok.com/@a', 'https://tiktok.com.evil/@a', 'https://user:pass@www.tiktok.com/@a', 'https://www.tiktok.com:444/@a', 'https://www.tiktok.com/@a/video/../1'):
            with self.assertRaises(ValueError): d.normalize_url(bad)
    def test_limit_includes_previously_downloaded(self):
        self.run_case(limit=1)
        e = FakeExtractor()
        report = self.run_case(e, limit=2)
        self.assertEqual(report['counts']['downloaded'], 1)
        self.assertEqual(report['counts']['skipped'], 1)
        self.assertEqual(len(list(self.output.rglob('*.mp4'))), 2)
        self.assertNotIn(('inspect', '3'), e.calls)
    def test_duplicate_batch_does_not_request_videos(self):
        self.run_case()
        e = FakeExtractor()
        report = self.run_case(e)
        self.assertEqual(report['counts']['skipped'], 3)
        self.assertEqual(e.calls, [])
    def test_missing_archive_file_is_downloaded_again(self):
        self.run_case(limit=1)
        next(self.output.rglob('*.mp4')).unlink()
        report = self.run_case(limit=1)
        self.assertEqual(report['counts']['downloaded'], 1)
    def test_mixed_login_real_failure_and_success(self):
        report = self.run_case(FakeExtractor(errors={'1': 'Login required', '2': 'parse error signed=https://cdn/?secret=abc'}))
        self.assertEqual(report['status'], 'PARTIAL')
        self.assertEqual([report['counts'][x] for x in ('downloaded', 'failed', 'login-required')], [1, 1, 1])
        self.assertNotIn('secret', (self.output / 'report.json').read_text())
    def test_pure_login_not_failed(self):
        report = self.run_case(FakeExtractor(ids=['1'], errors={'1': 'Please log in to view'}))
        self.assertEqual(report['status'], 'LOGIN_REQUIRED')
        self.assertEqual(report['counts']['failed'], 0)
    def test_stop_leaves_known_unattempted(self):
        for error, code in [('HTTP Error 429', 'RATE_LIMITED'), ('captcha required', 'VERIFICATION_REQUIRED')]:
            e = FakeExtractor(errors={'2': error})
            report = self.run_case(e)
            self.assertEqual(report['error']['code'], code)
            self.assertEqual(report['counts']['unattempted'], 1)
            self.assertNotIn(('inspect', '3'), e.calls)
    def test_discovery_empty_is_unknown_not_login(self):
        report = self.run_case(FakeExtractor(ids=[]))
        self.assertEqual(report['error']['code'], 'DISCOVERY_EMPTY')
        self.assertIsNone(report['discovered'])
        self.assertIsNone(report['counts']['unattempted'])
        self.assertEqual(report['counts']['login-required'], 0)
    def test_discovery_explicit_login_keeps_unknown_count(self):
        e = FakeExtractor()
        e.discover = lambda *_: (_ for _ in ()).throw(RuntimeError('login required'))
        report = self.run_case(e)
        self.assertEqual(report['status'], 'LOGIN_REQUIRED')
        self.assertIsNone(report['discovered'])
    def test_permission_parse_and_empty_are_not_login(self):
        for msg in ('HTTP Error 403', 'private video', 'unable to extract webpage', 'no videos found'):
            self.assertNotEqual(d.classify(RuntimeError(msg)), 'LOGIN_REQUIRED')
    def test_explicit_tiktok_login_messages(self):
        for msg in ('TikTok is requiring login for access to this content', "This user account is private. Log into an account that has access"):
            self.assertEqual(d.classify(RuntimeError(msg)), 'LOGIN_REQUIRED')
        self.assertEqual(d.classify(RuntimeError('Unable to extract secondary user ID')), 'PROFILE_ID_UNAVAILABLE')
        self.assertEqual(d.classify(RuntimeError("This user's account is likely either private or all videos private. Log into an account that has access")), 'ACCESS_DENIED')
    def test_network_retry_once_only(self):
        calls = []
        def fail(): calls.append(1); raise RuntimeError('connection timed out')
        with patch.object(d.time, 'sleep'), self.assertRaises(d.Failure): d.network_call(fail)
        self.assertEqual(len(calls), 2)
        calls.clear()
        def login(): calls.append(1); raise RuntimeError('login required')
        with self.assertRaises(d.Failure): d.network_call(login)
        self.assertEqual(len(calls), 1)
    def test_invalid_file_never_archived(self):
        def invalid(*_): raise d.Failure('VALIDATION_FAILED')
        report = self.run_case(limit=1, validator=invalid)
        self.assertEqual(report['counts']['failed'], 1)
        self.assertFalse((self.output / '.download-archive.json').exists())
        self.assertEqual(len(list(self.output.rglob('*.invalid-*'))), 1)
        self.assertEqual(self.run_case(limit=1)['counts']['downloaded'], 1)
    def test_interruption_resumes_parts(self):
        e = FakeExtractor()
        def interrupt(source, target):
            target.with_suffix('.mp4.part').write_bytes(b'partial-')
            raise KeyboardInterrupt
        e.download = interrupt
        report = self.run_case(e, limit=1)
        self.assertEqual(report['error']['code'], 'INTERRUPTED')
        self.assertFalse((self.output / '.download-archive.json').exists())
        resumed = FakeExtractor()
        self.assertEqual(self.run_case(resumed, limit=1)['counts']['downloaded'], 1)
        self.assertTrue(resumed.resumed)
        self.assertNotIn(('inspect', '1'), resumed.calls)
    def test_recover_completed_file_before_archive(self):
        def interrupt(*_): raise KeyboardInterrupt
        self.run_case(limit=1, validator=interrupt)
        e = FakeExtractor()
        report = self.run_case(e, limit=1)
        self.assertTrue(report['results'][0]['recovered'])
        self.assertEqual(e.calls, [])
    def test_photo_live_list_only_and_no_auth(self):
        for suffix, reason in [('/photo/1', 'UNSUPPORTED_PHOTO'), ('/live', 'UNSUPPORTED_LIVE')]:
            report = self.run_case(url=PROFILE + suffix)
            self.assertEqual(report['results'][0]['reasonCode'], reason)
        e = FakeExtractor()
        self.assertEqual(self.run_case(e, list_only=True, limit=2)['counts']['listed'], 2)
        self.assertEqual(e.calls, [])
        opts = d.Extractor('ffmpeg').options()
        self.assertIsNone(opts['cookiesfrombrowser'])
        self.assertIsNone(opts['cookiefile'])
        self.assertFalse(opts['usenetrc'])
    def test_literal_percent_filename_reaches_yt_dlp(self):
        import yt_dlp
        with patch.object(yt_dlp, 'YoutubeDL') as cls:
            d.Extractor('ffmpeg').download(d.normalize_url(PROFILE + '/video/1'), Path('/tmp/100%_[1].mp4'))
            self.assertEqual(cls.call_args.args[0]['outtmpl'], '/tmp/100%%_[1].mp4')
    def test_real_audio_video_decode_and_invalid_file(self):
        ffmpeg = d.ffmpeg_binary()
        target = self.output / 'test.mp4'
        subprocess.run([ffmpeg, '-nostdin', '-v', 'error', '-f', 'lavfi', '-i', 'color=size=64x64:rate=10', '-f', 'lavfi', '-i', 'sine=frequency=1000', '-t', '0.5', '-c:v', 'libx264', '-c:a', 'aac', str(target)], check=True)
        result = d.validate_file(target, ffmpeg)
        self.assertTrue(result['decodePassed'])
        self.assertEqual(result['width'], 64)
        self.assertEqual(result['audioCodec'], 'aac')
        target.write_bytes(b'not a video')
        with self.assertRaises(d.Failure): d.validate_file(target, ffmpeg)

if __name__ == '__main__': unittest.main()
